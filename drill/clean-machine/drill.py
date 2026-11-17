#!/usr/bin/env python3
"""Clean-machine release drill for lu-machinery: install, run every consumer row, roll back, rerun.

Run it with a plain CPython 3.12+ on Linux, with no machinery checkout anywhere on the machine:

    python3 -I drill.py all --workdir /path/to/empty/dir --version 0.1.28 --rollback-to 0.1.27

Steps (``all``):
  1. setup     download both release wheels and SHA256SUMS from GitHub, check the sums,
               create a fresh venv and install the forward version (dependencies from PyPI);
               check every installed file against the wheel's RECORD.
  2. world     build the mock world: a synthetic product checkout with its own bare remote,
               fixture stores, stub command-line tools (gh, systemctl, tmux, sudo, provider CLIs)
               and two loopback stub servers (a Sources stand-in and a Monitor stand-in).
  3. run       run every row in rows.json against the installed release; one JSON receipt per row.
  4. rollback  install the previous release into the same venv and check RECORD again.
  5. rerun     run every row again; one JSON receipt per row.
  6. forward   install the forward release again and check RECORD (roll-forward).

Each row runs under ``strace -f`` when strace is available (required unless --no-strace): any code
file (``.py``, ``.pyc``, ``.so``, ``.pth``, ``.sh`` or an executed file) whose path has a
``lu_machinery`` or ``agents_extensions`` segment outside the venv's site-packages fails the row
(accidental dependence on a machinery copy outside the release).

Exit codes: 0 every row passed in both phases; 1 at least one row failed; 2 setup or usage error.
Network: GitHub release downloads and PyPI only; rows talk only to the loopback stubs.
This script imports nothing from lu_machinery; it only drives the venv's interpreter.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

KIT = Path(__file__).resolve().parent
RELEASE_URL = (
    "https://github.com/learn-ukrainian/machinery/releases/download/v{version}/{name}"
)
DIST = "lu_machinery"
RECEIPT_SCHEMA = "lu-clean-machine-drill-receipt/1"
SUMMARY_SCHEMA = "lu-clean-machine-drill-summary/1"
CODE_SUFFIXES = (".py", ".pyc", ".so", ".pth", ".sh")
GUARDED_SEGMENTS = ("lu_machinery", "agents_extensions")
OUTPUT_TAIL = 4000


class DrillError(Exception):
    """A setup step failed; the message is safe to print."""


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"[drill {now_utc()}] {message}", flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# Release download, install and integrity
# ---------------------------------------------------------------------------------------------


def wheel_name(version: str) -> str:
    return f"{DIST}-{version}-py3-none-any.whl"


def fetch(url: str, dest: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "lu-clean-machine-drill"}
    )
    with (
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
        urllib.request.urlopen(
            request, timeout=120
        ) as response,  # fixed https GitHub release URL
        dest.open("wb") as out,
    ):
        shutil.copyfileobj(response, out)


def obtain_wheel(version: str, wheel_dir: Path, offline_dir: Path | None) -> Path:
    """Download (or copy) one release wheel with its SHA256SUMS and check the wheel's sum."""
    target = wheel_dir / f"v{version}"
    target.mkdir(parents=True, exist_ok=True)
    name = wheel_name(version)
    for item in (name, "SHA256SUMS"):
        dest = target / item
        if dest.exists():
            continue
        if offline_dir is not None:
            source = offline_dir / f"v{version}" / item
            if not source.is_file():
                raise DrillError(f"--wheel-dir has no {source}")
            shutil.copyfile(source, dest)
        else:
            log(f"download v{version} {item}")
            fetch(RELEASE_URL.format(version=version, name=item), dest)
    sums: dict[str, str] = {}
    for line in (target / "SHA256SUMS").read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0]
    expected = sums.get(name)
    actual = sha256_file(target / name)
    if expected != actual:
        raise DrillError(
            f"v{version} wheel sha256 {actual} does not match SHA256SUMS {expected}"
        )
    return target / name


def run_checked(
    argv: list[str], *, env: dict[str, str], cwd: Path, what: str, timeout: int = 1800
) -> str:
    result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit - argv list, no shell
        argv,
        check=False,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise DrillError(
            f"{what} exited {result.returncode}: {(result.stdout + result.stderr)[-2000:]}"
        )
    return result.stdout


PYPI_INDEX = "https://pypi.org/simple"


def base_env(home: Path) -> dict[str, str]:
    """Setup environment: home and temp files inside the world, pip limited to PyPI.

    ``PIP_CONFIG_FILE=/dev/null`` makes pip skip every configuration file, including a
    machine-wide one that could name another index or carry credentials.
    """
    tmp = home.parent / "setup-tmp"
    scratch_root = home.parent / "scratchroot"
    for path in (tmp, scratch_root):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "LU_SCRATCH_ROOT": str(scratch_root),
        "LU_RUNTIME_TMP_BASE_ROOT": str(scratch_root),
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(tmp),
        "TMP": str(tmp),
        "TEMP": str(tmp),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_INDEX_URL": PYPI_INDEX,
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
    }


def create_venv(python: str, venv: Path, home: Path) -> Path:
    run_checked(
        [python, "-I", "-m", "venv", str(venv)],
        env=base_env(home),
        cwd=venv.parent,
        what="venv",
    )
    return venv / "bin" / "python"


def install_wheel(py: Path, wheel: Path, home: Path) -> None:
    run_checked(
        [
            str(py),
            "-I",
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-input",
            "--index-url",
            PYPI_INDEX,
            str(wheel),
        ],
        env=base_env(home),
        cwd=py.parent,
        what=f"pip install {wheel.name}",
    )


INTEGRITY_PROBE = r"""
import base64, hashlib, importlib.metadata as md, json, sys, sysconfig
from pathlib import Path
want = sys.argv[1]
dist = md.distribution("lu-machinery")
site = Path(sysconfig.get_paths()["purelib"]).resolve()
problems, checked = [], 0
listed = set()
for item in dist.files or []:
    path = Path(item.locate()).resolve()
    listed.add(path)
    if item.hash is None:
        continue
    data = path.read_bytes() if path.is_file() else None
    if data is None:
        problems.append(f"missing {item}")
        continue
    got = base64.urlsafe_b64encode(hashlib.new(item.hash.mode, data).digest()).rstrip(b"=").decode()
    checked += 1
    if got != item.hash.value:
        problems.append(f"hash {item}")
# Files under the package roots that RECORD does not list are left-overs from another version.
for top in ("lu_machinery", "agents_extensions"):
    root = site / top
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.resolve() not in listed:
                problems.append(f"unlisted {path.relative_to(site)}")
import lu_machinery
origin = Path(lu_machinery.__file__).resolve()
if site not in origin.parents:
    problems.append("lu_machinery imported from outside site-packages")
print(json.dumps({"version": dist.version, "want": want, "checked": checked, "problems": problems[:50],
                  "problem_count": len(problems)}))
sys.exit(0 if dist.version == want and not problems else 1)
"""


def check_integrity(py: Path, version: str, home: Path) -> dict[str, Any]:
    result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit - argv list, no shell
        [str(py), "-I", "-c", INTEGRITY_PROBE, version],
        check=False,
        env=base_env(home),
        cwd=home,
        capture_output=True,
        text=True,
        timeout=300,
    )
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        report = {"error": (result.stdout + result.stderr)[-2000:]}
    report["ok"] = result.returncode == 0
    return report


# ---------------------------------------------------------------------------------------------
# Mock world
# ---------------------------------------------------------------------------------------------

STUB_TOOLS = (
    "gh",
    "systemctl",
    "tmux",
    "sudo",
    "codex",
    "claude",
    "agy",
    "cursor-agent",
    "journalctl",
    "loginctl",
    "opencode",
)

STUB_SOURCE = r'''
"""Drill stub for {tool}: answers from answers.json, logs each call to calls.jsonl."""
import json, os, sys, time
from pathlib import Path
STATE = Path(os.environ.get("DRILL_STUB_STATE") or Path(__file__).resolve().parent.parent / "state")
TOOL = {tool!r}
argv = sys.argv[1:]
STATE.mkdir(parents=True, exist_ok=True)
with (STATE / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps({{"tool": TOOL, "argv": argv, "t": time.time()}}) + "\n")
answers = json.loads((STATE / "answers.json").read_text()).get(TOOL, [])
if TOOL == "sudo":
    # User-level stand-in for sudo: drop sudo's own options and run the command as this user.
    rest = list(argv)
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U") and rest:
            rest.pop(0)
        if flag == "--":
            break
    if not rest:
        sys.exit(0)
    os.execvp(rest[0], rest)
joined = " ".join(argv)
if TOOL == "opencode" and argv[:1] == ["db"]:
    # OpenCode CLI stand-in: `db path` and `db <SQL> --format json` against the fixture store.
    import sqlite3
    store = Path(os.environ["HOME"]) / ".local" / "share" / "opencode" / "opencode.db"
    if argv[1:2] == ["path"]:
        print(store)
        sys.exit(0)
    connection = sqlite3.connect(store)
    connection.row_factory = sqlite3.Row
    rows = [dict(row) for row in connection.execute(argv[1]).fetchall()]
    connection.commit()
    print(json.dumps(rows))
    sys.exit(0)
if TOOL == "gh" and argv[:1] == ["api"]:
    # GitHub REST/GraphQL stand-in: route on the endpoint, frame like `gh api --include`.
    import re
    rest, endpoint, method, i = argv[1:], None, "GET", 0
    takes_value = {{"--method", "-X", "-H", "--header", "--hostname", "--input", "-f", "-F", "--field", "--raw-field", "--jq", "-q", "--template", "-t", "--cache"}}
    while i < len(rest):
        item = rest[i]
        if item in ("--method", "-X") and i + 1 < len(rest):
            method = rest[i + 1]
        if item in takes_value:
            i += 2
            continue
        if not item.startswith("-") and endpoint is None:
            endpoint = item
        i += 1
    payload = sys.stdin.read() if "--input" in rest else ""
    with (STATE / "calls.jsonl").open("a") as handle:
        handle.write(json.dumps({{"tool": "gh-api", "method": method, "endpoint": endpoint, "query": payload[:400]}}) + "\n")
    routes = json.loads((STATE / "answers.json").read_text()).get("gh_api", [])
    status, body = 200, "[]"
    for route in routes:
        if re.search(route["endpoint_re"], (endpoint or "").split("?")[0].lstrip("/")) and re.search(route.get("query_re", ""), payload) and route.get("method", method) == method:
            status, body = route.get("status", 200), json.dumps(route["body"])
            break
    if "--include" in rest or "-i" in rest:
        resource = "graphql" if (endpoint or "").startswith("graphql") else "core"
        sys.stdout.write(f"HTTP/2.0 {{status}} OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                         f"X-Ratelimit-Limit: 5000\r\nX-Ratelimit-Remaining: 4999\r\nX-Ratelimit-Reset: 4102444800\r\n"
                         f"X-Ratelimit-Used: 1\r\nX-Ratelimit-Resource: {{resource}}\r\n\r\n")
    sys.stdout.write(body)
    sys.exit(0 if status < 400 else 1)
for answer in answers:
    if all(part in argv or part in joined for part in answer.get("match", [])):
        sys.stdout.write(answer.get("stdout", ""))
        sys.stderr.write(answer.get("stderr", ""))
        sys.exit(answer.get("rc", 0))
default = json.loads((STATE / "answers.json").read_text()).get("_default", {{}}).get(TOOL, {{"stdout": "", "rc": 0}})
sys.stdout.write(default.get("stdout", ""))
sys.exit(default.get("rc", 0))
'''


def stub_answers(world: Path) -> dict[str, Any]:
    """Known answers the stub tools return. Keys are tool names; each answer matches argv parts."""
    show = "ActiveState=active\nSubState=running\nMainPID=4242\nExecMainStartTimestampMonotonic=1000\n"
    return {
        "gh": [
            {"match": ["auth", "token"], "stdout": "drill-fake-token\n"},
            {
                "match": ["auth", "status"],
                "stdout": "Logged in to github.com as drill-bot (fake)\n",
            },
            {"match": ["--version"], "stdout": "gh version 0.0.0-drill-stub\n"},
            {
                "match": ["repo", "view"],
                "stdout": json.dumps(
                    {
                        "nameWithOwner": "drill/product",
                        "defaultBranchRef": {"name": "main"},
                        "visibility": "PUBLIC",
                    }
                )
                + "\n",
            },
            {"match": ["pr", "list"], "stdout": "[]\n"},
            {
                "match": ["issue", "list"],
                "stdout": json.dumps(
                    [
                        {
                            "number": 1,
                            "title": "drill: synthetic issue",
                            "body": "## Outcome\nA synthetic issue for the clean-machine drill.\n",
                            "labels": [],
                            "createdAt": "2026-01-01T00:00:00Z",
                            "updatedAt": "2026-01-01T00:00:00Z",
                            "url": "https://example.invalid/1",
                            "state": "OPEN",
                        }
                    ]
                )
                + "\n",
            },
            {
                "match": ["issue", "comment"],
                "stdout": "https://example.invalid/drill/product/issues/1#issuecomment-1\n",
            },
            {
                "match": ["api", "graphql"],
                "stdout": json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequests": {"nodes": []},
                                "mergeQueue": {"entries": {"nodes": []}},
                            }
                        }
                    }
                )
                + "\n",
            },
            {"match": ["api"], "stdout": "[]\n"},
        ],
        "gh_api": [
            {
                "endpoint_re": "^graphql$",
                "method": "POST",
                "body": {
                    "data": {
                        "rateLimit": {
                            "remaining": 4999,
                            "cost": 1,
                            "resetAt": "2100-01-01T00:00:00Z",
                        },
                        "repository": {"__typename": "Repository"},
                    }
                },
            },
            {
                "endpoint_re": r"^repos/[^/]+/[^/]+/issues",
                "body": [
                    {
                        "number": 1,
                        "title": "drill: synthetic issue",
                        "state": "open",
                        "body": "## Outcome\nSynthetic.\n",
                        "labels": [],
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "html_url": "https://example.invalid/drill/product/issues/1",
                        "user": {"login": "drill-bot"},
                    }
                ],
            },
            {
                "endpoint_re": r"^repos/[^/]+/[^/]+$",
                "body": {
                    "full_name": "drill/product",
                    "default_branch": "main",
                    "private": False,
                    "visibility": "public",
                },
            },
            {
                "endpoint_re": r"^repos/[^/]+/[^/]+/pulls/\d+$",
                "body": {
                    "number": 1,
                    "state": "closed",
                    "merged": True,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "draft": False,
                    "title": "drill: synthetic merged PR",
                    "user": {"login": "drill-bot"},
                    "head": {
                        "ref": "drill/linked",
                        "sha": "0" * 40,
                        "repo": {"full_name": "drill/product"},
                    },
                    "base": {"ref": "main", "repo": {"full_name": "drill/product"}},
                    "merge_commit_sha": "1" * 40,
                    "html_url": "https://github.com/drill/product/pull/1",
                    "url": "https://api.github.com/repos/drill/product/pulls/1",
                },
            },
            {
                "endpoint_re": "^user$",
                "body": {"login": "drill-bot", "id": 1, "type": "User"},
            },
            {
                "endpoint_re": "^rate_limit",
                "body": {
                    "resources": {
                        "core": {
                            "limit": 5000,
                            "remaining": 4999,
                            "reset": 4102444800,
                            "used": 1,
                        },
                        "graphql": {
                            "limit": 5000,
                            "remaining": 4999,
                            "reset": 4102444800,
                            "used": 1,
                        },
                    }
                },
            },
        ],
        "systemctl": [
            {
                "match": ["show", "lu-dispatch.slice"],
                "stdout": "LoadState=loaded\nActiveState=inactive\nMemoryCurrent=0\nMemoryMax=infinity\nControlGroup=\n",
            },
            {"match": ["show"], "stdout": show},
            {"match": ["is-active"], "stdout": "active\n"},
            {"match": ["is-enabled"], "stdout": "enabled\n"},
            {"match": ["list-units"], "stdout": ""},
            {"match": ["list-timers"], "stdout": ""},
        ],
        "tmux": [
            {"match": ["has-session"], "rc": 1, "stderr": "can't find session\n"},
            {"match": ["list-sessions"], "rc": 1, "stderr": "no server running\n"},
            {"match": ["-V"], "stdout": "tmux 3.4-drill-stub\n"},
        ],
        "codex": [{"match": ["--version"], "stdout": "codex-cli 0.0.0-drill-stub\n"}],
        "claude": [
            {"match": ["--version"], "stdout": "0.0.0-drill-stub (Claude Code)\n"}
        ],
        "agy": [{"match": ["--version"], "stdout": "agy 0.0.0-drill-stub\n"}],
        "cursor-agent": [{"match": ["--version"], "stdout": "0.0.0-drill-stub\n"}],
        "_default": {
            "gh": {"stdout": "[]\n", "rc": 0},
            "codex": {"stdout": "DRILL-STUB-PROVIDER-OK\n", "rc": 0},
            "claude": {"stdout": "DRILL-STUB-PROVIDER-OK\n", "rc": 0},
            "agy": {"stdout": "DRILL-STUB-PROVIDER-OK\n", "rc": 0},
            "cursor-agent": {"stdout": "DRILL-STUB-PROVIDER-OK\n", "rc": 0},
        },
    }


ADMISSION_POLICY = {
    "schema": "learn-ukrainian.product-admission-policy",
    "schema_version": 1,
    "origin": "scripts/curriculum/admission_policy.v1.json",
    "seats": {
        "content_agent_types": ["curriculum-orchestrator"],
        "ukrainian_content_families": ["anthropic", "google", "openai"],
        "ukrainian_review_seats": [
            {"route": "agy", "family": "google"},
            {"route": "codex", "family": "openai"},
        ],
    },
    "receipts": {
        "learner_payload_paths": [
            "curriculum/",
            "scripts/curriculum/",
            "scripts/data/stress_overrides.yaml",
            "scripts/verification/stress*",
            "scripts/pipeline/stress_annotator.py",
            "scripts/lexicon/",
            "site/src/lib/lexicon/",
            "scripts/review/prompts/",
            "scripts/build/phases/",
            "scripts/build/fresh/prompt*",
            "docs/epics/fresh-build-*",
            "schemas/activities-*",
        ],
        "review_receipts_by_author_family": {
            "google": ["openai"],
            "openai": ["google"],
        },
        "review_receipts_default": ["google", "openai"],
    },
    "content_sha256": "1ccc53ccd3aa2409611673006bcfe0ed76c6d0b4591878399c72f6b60093ff56",
}

FIXTURES = KIT / "fixtures"


def sources_server_standin(tools: dict[str, list[str]]) -> str:
    """Declaration-only stand-in for the product's Sources MCP server, from the extracted tool list."""
    calls = [f"_tool(name={name!r})" for name in tools["read_only"]]
    calls += [
        f"_tool(name={name!r}, annotations=PERSISTING)" for name in tools["persisting"]
    ]
    body = ",\n        ".join(calls)
    return (
        '"""Drill stand-in for the product\'s Sources MCP server: tool declarations only.\n\n'
        "Generated from fixtures/sources-tools.json (names parsed from the real server, never executed).\n"
        '"""\n\n\n'
        "class ToolAnnotations:\n    def __init__(self, **hints):\n        self.hints = hints\n\n\n"
        "READ_ONLY = ToolAnnotations(readOnlyHint=True)\nPERSISTING = ToolAnnotations(readOnlyHint=False)\n\n\n"
        'def _tool(**kwargs):\n    kwargs.setdefault("annotations", READ_ONLY)\n    return kwargs\n\n\n'
        f"async def list_tools():\n    return [\n        {body},\n    ]\n"
    )


START_CLAUDE_STANDIN = """#!/usr/bin/env bash
# Drill stand-in for the product's start script: it sources the installed release's launcher core.
set -euo pipefail
core="$("$LEARN_UKRAINIAN_PYTHON" -I -c 'from importlib.resources import files; print(files("lu_machinery").joinpath("lib/launcher_core.sh"))')"
source "$core"
launcher_main claude "${DRILL_LAUNCH_MODE:-interactive}" "$@"
"""


PRODUCT_API_STANDIN = """\"\"\"Drill stand-in for the product's Monitor contribution: it adds nothing.\"\"\"

from lu_machinery.api.product_contribution import ProductContribution


def build_api(context):
    return ProductContribution()
"""

HANG_DIAGNOSTICS_STANDIN = """\"\"\"Drill stand-in for the product's hang-diagnostics helpers used by the Sources watchdog.\"\"\"

import signal
from pathlib import Path

DUMP_SIGNAL = signal.SIGUSR1


def resolve_dump_dir(root):
    return Path(root) / "batch_state" / "sources-hang-dumps"


def ensure_dump_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(path)


def faulthandler_log_path(dump_dir, pid):
    return Path(dump_dir) / f"faulthandler-{pid}.log"
"""


def write_stubs(world: Path, base_python: str) -> Path:
    bindir = world / "stubs" / "bin"
    state = world / "stubs" / "state"
    bindir.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    (state / "answers.json").write_text(json.dumps(stub_answers(world), indent=1))
    libdir = world / "stubs" / "lib"
    libdir.mkdir(parents=True, exist_ok=True)
    for tool in STUB_TOOLS:
        body = libdir / f"{tool}.py"
        body.write_text(STUB_SOURCE.format(tool=tool))
        # A POSIX sh launcher, not a shebang on the interpreter: any path, spaces included, works.
        path = bindir / tool
        path.write_text(
            f'#!/bin/sh\nexec {shlex.quote(base_python)} -I {shlex.quote(str(body))} "$@"\n'
        )
        path.chmod(0o755)
    return bindir


def git(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    return run_checked(
        ["git", *args], env=env, cwd=cwd, what="git " + " ".join(args[:2])
    )


def git_env(home: Path) -> dict[str, str]:
    env = base_env(home)
    env.update(
        {
            "GIT_AUTHOR_NAME": "drill",
            "GIT_AUTHOR_EMAIL": "drill@example.invalid",
            "GIT_COMMITTER_NAME": "drill",
            "GIT_COMMITTER_EMAIL": "drill@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        }
    )
    return env


def task_record(
    index: int, extract: dict[str, str], finished: datetime, product: Path
) -> dict[str, Any]:
    """One dispatcher record: scrubbed real status, mode, agent and model; synthetic id, path and times."""
    stamp = finished.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": 2,
        "task_id": task_id_for(index),
        **{key: extract[key] for key in ("agent", "model", "status", "mode")},
        "created_at": stamp,
        "started_at": stamp,
        "finished_at": stamp,
        "updated_at": stamp,
        "repo": "product",
        "cwd": str(product),
        "worker_pid": None,
    }


def task_id_for(index: int) -> str:
    """Records 1 and 2 are thirty days old; record 3 finished now."""
    return f"drill-old-{index}" if index < 3 else f"drill-fresh-{index}"


def build_world(workdir: Path, base_python: str) -> dict[str, Path]:
    """Create the synthetic product, its remote, fixture stores and stubs. Idempotent per workdir."""
    world = workdir / "world"
    if world.exists():
        shutil.rmtree(world)
    home = world / "home"
    product = world / "product"
    remote = world / "remotes" / "product.git"
    for path in (home, product, remote.parent, world / "fixtures"):
        path.mkdir(parents=True, exist_ok=True)
    env = git_env(home)
    (home / ".gitconfig").write_text(
        "[init]\n\tdefaultBranch = main\n[safe]\n\tdirectory = *\n"
    )
    stubs = write_stubs(world, base_python)

    # Synthetic product checkout: the product marker, a stub backup script, task records.
    (product / "curriculum" / "l2-uk-en").mkdir(parents=True)
    (product / "curriculum" / "l2-uk-en" / "curriculum.yaml").write_text(
        "# drill stand-in curriculum\nlevels: []\n"
    )
    (product / "scripts").mkdir()
    backup = product / "scripts" / "backup-data.sh"
    backup.write_text(
        '#!/usr/bin/env bash\n# Drill stand-in for the product backup: no data leaves the machine.\necho "drill backup stub: $*"\nexit 0\n'
    )
    backup.chmod(0o755)
    (product / "README.md").write_text(
        "Synthetic product checkout for the lu-machinery clean-machine drill.\n"
    )
    (product / ".gitignore").write_text("batch_state/\nlogs/\n.deploy-state/\n")
    (product / "scripts" / "curriculum").mkdir()
    (product / "scripts" / "curriculum" / "admission_policy.v1.json").write_text(
        json.dumps(ADMISSION_POLICY, indent=2) + "\n"
    )
    (product / ".mcp" / "servers" / "sources").mkdir(parents=True)
    (product / ".mcp" / "servers" / "sources" / "server.py").write_text(
        sources_server_standin(
            json.loads((FIXTURES / "sources-tools.json").read_text())
        )
    )
    start = product / "start-claude.sh"
    start.write_text(START_CLAUDE_STANDIN)
    start.chmod(0o755)
    for package in ("scripts", "scripts/curriculum", "scripts/rag"):
        (product / package).mkdir(exist_ok=True)
        (product / package / "__init__.py").write_text("")
    (product / "scripts" / "curriculum" / "monitor_paths.v1.json").write_text(
        json.dumps(
            {
                "schema": "learn-ukrainian.monitor-paths",
                "schema_version": 1,
                "product_api": "scripts.curriculum.drill_product_api",
            },
            indent=1,
        )
        + "\n"
    )
    (product / "scripts" / "curriculum" / "drill_product_api.py").write_text(
        PRODUCT_API_STANDIN
    )
    (product / "scripts" / "rag" / "hang_diagnostics.v1.json").write_text(
        json.dumps(
            {
                "schema": "learn-ukrainian.hang-diagnostics",
                "schema_version": 1,
                "origin": "scripts/rag/hang_diagnostics.v1.json",
                "module": "scripts.rag.drill_hang_diagnostics",
            },
            indent=1,
        )
        + "\n"
    )
    (product / "scripts" / "rag" / "drill_hang_diagnostics.py").write_text(
        HANG_DIAGNOSTICS_STANDIN
    )
    (product / "tests").mkdir()
    (product / "tests" / "test_drill_data.py").write_text(
        "def test_drill_data_tier_runs():\n    assert 'мова'.isalpha()\n\n\n"
        "def test_drill_known_failure():\n    assert 'мова' == 'language', 'known failure, tracked as issue 1'\n"
    )
    (product / "scripts" / "ci").mkdir()
    nodeids = [
        "tests/test_drill_data.py::test_drill_data_tier_runs",
        "tests/test_drill_data.py::test_drill_known_failure",
    ]
    (product / "scripts" / "ci" / "data_tier_selection.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "nodeids": nodeids,
                "files": ["tests/test_drill_data.py"],
                "class_c_merge_group": 2,
            },
            indent=1,
        )
        + "\n"
    )
    (product / "scripts" / "ci" / "data_tier_known_failures.json").write_text(
        json.dumps({"known_failures": {nodeids[1]: 1}}, indent=1) + "\n"
    )
    tasks = product / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    old = datetime.now(UTC) - timedelta(days=30)
    extracts = json.loads((FIXTURES / "task-records.json").read_text())
    for index, extract in enumerate(extracts[:3], start=1):
        finished = old if index < 3 else datetime.now(UTC)
        record = task_record(index, extract, finished, product)
        (tasks / f"{record['task_id']}.json").write_text(json.dumps(record, indent=1))
    git(["init", "-q", "-b", "main"], product, env)
    git(["add", "-A"], product, env)
    git(["commit", "-q", "-m", "drill: synthetic product"], product, env)
    (product / "CHANGELOG.md").write_text(
        "# Synthetic product changelog\n\n- drill: second commit\n"
    )
    git(["add", "CHANGELOG.md"], product, env)
    git(["commit", "-q", "-m", "drill: second commit"], product, env)
    git(["init", "-q", "--bare", str(remote)], remote.parent, env)
    git(["remote", "add", "origin", str(remote)], product, env)
    git(["push", "-q", "origin", "main"], product, env)
    worktree = world / "product-worktree"
    git(["worktree", "add", "-q", "-b", "drill/linked", str(worktree)], product, env)

    # Session-store fixture for the digest rows (synthetic Codex rollout lines).
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "01"
    sessions.mkdir(parents=True)
    lines = [
        {
            "type": "session_meta",
            "payload": {"id": "drill-session", "cwd": str(product)},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "drill: run the synthetic task"}
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "drill: synthetic answer, PR #1 opened",
                    }
                ],
            },
        },
    ]
    (sessions / "rollout-2026-01-01T00-00-00-drill.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )

    # Sources stand-in data: the scrubbed VESUM sample (word form, lemma, part of speech, tags).
    shutil.copyfile(
        FIXTURES / "vesum-sample.tsv", world / "fixtures" / "vesum-standin.tsv"
    )
    (world / "tmp").mkdir()
    (world / "scratchroot").mkdir(exist_ok=True)
    opencode = home / ".local" / "share" / "opencode"
    opencode.mkdir(parents=True)
    import sqlite3

    with contextlib.closing(sqlite3.connect(opencode / "opencode.db")) as db:
        db.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, time_updated INTEGER)"
        )
        stamp = int((datetime.now(UTC) - timedelta(days=60)).timestamp() * 1000)
        db.executemany(
            "INSERT INTO session VALUES (?, ?, ?)",
            [
                ("ses_drillold", None, stamp),
                ("ses_drillnew", None, int(time.time() * 1000)),
            ],
        )
        db.commit()
    return {
        "world": world,
        "home": home,
        "product": product,
        "remote": remote,
        "stubs": stubs,
        "worktree": worktree,
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    """Loopback stand-in: fixed GET routes, POSTs acknowledged, every request logged as one JSON line."""

    routes: ClassVar[dict[str, tuple[int, str, bytes]]] = {}
    log_path: ClassVar[Path | None] = None
    vesum: ClassVar[frozenset[str]] = frozenset()

    def log_message(self, *args: Any) -> None:  # quiet
        return

    def _record(self, method: str, body: bytes) -> None:
        if self.log_path is not None:
            with self.log_path.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "method": method,
                            "path": self.path,
                            "bytes": len(body),
                            "t": time.time(),
                        }
                    )
                    + "\n"
                )

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._record("GET", b"")
        path = urllib.parse.urlsplit(self.path).path
        self._send(
            *self.routes.get(
                path,
                (404, "application/json", b'{"error":"not found in drill stand-in"}'),
            )
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self._record("POST", raw)
        path = urllib.parse.urlsplit(self.path).path
        if path == "/mcp":
            self._mcp(raw)
            return
        self._send(
            200,
            "application/json",
            json.dumps({"received": True, "drill_standin": True}).encode(),
        )

    def _mcp(self, raw: bytes) -> None:
        """Minimal JSON-RPC answer: a tool call's words are checked against the VESUM stand-in list."""
        try:
            request = json.loads(raw or b"{}")
        except ValueError:
            request = {}
        arguments = (request.get("params") or {}).get("arguments") or {}
        words = arguments.get("words") or (
            [arguments["word"]] if "word" in arguments else []
        )
        result = {word: word in self.vesum for word in words}
        body = {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {
                "structuredContent": result,
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                ],
            },
        }
        self._send(
            200, "application/json", json.dumps(body, ensure_ascii=False).encode()
        )


@contextlib.contextmanager
def stub_server(
    port: int,
    routes: dict[str, tuple[int, str, bytes]],
    log_path: Path,
    vesum: frozenset[str],
) -> Iterator[str]:
    """Serve a stand-in on 127.0.0.1:port (0 picks a free port). A busy port is a setup error."""
    handler = type(
        "Handler", (_Handler,), {"routes": routes, "log_path": log_path, "vesum": vesum}
    )
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise DrillError(
            f"127.0.0.1:{port} is busy ({exc}); a clean machine runs no Monitor or Sources service there. "
            "Use the bwrap isolation (default when bwrap is installed) or stop that service."
        ) from exc
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


MONITOR_PORT = 8765
SOURCES_PORT = 8766


def sources_routes() -> dict[str, tuple[int, str, bytes]]:
    health = {
        "status": "ok",
        "service": "drill-sources-standin",
        "corpus": "vesum-sample",
    }
    return {"/health": (200, "application/json", json.dumps(health).encode())}


def monitor_routes() -> dict[str, tuple[int, str, bytes]]:
    budget = {"generated_at": now_utc(), "agents": {}}
    return {
        "/api/health": (
            200,
            "application/json",
            json.dumps({"status": "ok", "service": "drill-monitor-standin"}).encode(),
        ),
        "/api/state/routing-budget": (
            200,
            "application/json",
            json.dumps(budget).encode(),
        ),
    }


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------


def load_rows(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    probes = data["probes"]
    for row in data["rows"]:
        if row["probe"] not in probes:
            raise DrillError(f"row {row['id']} names unknown probe {row['probe']}")
        if row["class"] not in data["classes"]:
            raise DrillError(f"row {row['id']} has unknown class {row['class']}")
    return data


def expand(value: str, names: dict[str, str]) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda m: names.get(m.group(1), m.group(0)), value)


def strace_usable() -> bool:
    if shutil.which("strace") is None:
        return False
    result = subprocess.run(
        ["strace", "-f", "-o", "/dev/null", "--", "/bin/true"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


_OPEN_DONE = re.compile(
    r"\b(?:open|openat|openat2)\(.*= \d+<(.*)>\s*$|<\.\.\. (?:open|openat|openat2) resumed>.*= \d+<(.*)>\s*$"
)
_EXEC_CALL = re.compile(
    r'^(\d+)\s+(execve|execveat)\((?:(?:AT_FDCWD|\d+)<((?:[^>\\]|\\.)*)>, )?"((?:[^"\\]|\\.)*)"'
)
_EXEC_RESUMED = re.compile(r"^(\d+)\s+<\.\.\. execve(?:at)? resumed>.*\) = 0\s*$")
_ESCAPES = {
    "n": 10,
    "t": 9,
    "r": 13,
    "v": 11,
    "f": 12,
    "a": 7,
    "b": 8,
    "\\": 92,
    '"': 34,
    "'": 39,
}


def strace_bytes(text: str) -> bytes:
    """Undo strace's C-style escaping (octal, hex and single-letter escapes) back to raw bytes."""
    out = bytearray()
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 == len(text):
            out += char.encode("utf-8", "surrogateescape")
            index += 1
            continue
        nxt = text[index + 1]
        if nxt == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", text[index + 2 : index + 4]):
            out.append(int(text[index + 2 : index + 4], 16))
            index += 4
        elif nxt in "01234567":
            digits = re.match(r"[0-7]{1,3}", text[index + 1 :]).group(0)
            out.append(int(digits, 8) & 0xFF)
            index += 1 + len(digits)
        else:
            out.append(_ESCAPES.get(nxt, ord(nxt)))
            index += 2
    return bytes(out)


def origin_findings(trace: Path, site: Path) -> tuple[list[str], int]:
    """Code files with a guarded segment outside site-packages, and how many release files ran.

    The trace comes from ``strace -f -y``: a successful open carries the opened file's resolved
    path as ``= fd</path>`` (on the completed or the resumed line), so relative paths and
    directory descriptors need no reconstruction and failed or unfinished calls never count. An
    exec counts only once it returned 0; a relative exec path stays relative and so cannot
    prove that it came from the release.
    """
    if not trace.exists():
        return ["trace missing"], 0
    site_real = os.path.realpath(site)
    seen: set[str] = set()
    events: list[tuple[str, bool]] = []
    pending: dict[str, str] = {}
    for line in trace.read_text(
        encoding="utf-8", errors="surrogateescape"
    ).splitlines():
        opened = _OPEN_DONE.search(line)
        if opened:
            events.append(
                (
                    opened.group(1) if opened.group(1) is not None else opened.group(2),
                    False,
                )
            )
            continue
        call = _EXEC_CALL.match(line)
        if call:
            pid, _, base, raw = call.groups()
            path = raw if base is None or raw.startswith("/") else base + "/" + raw
            if line.rstrip().endswith(") = 0"):
                events.append((path, True))
            elif "<unfinished ...>" in line:
                pending[pid] = path
            continue
        resumed = _EXEC_RESUMED.match(line)
        if resumed and resumed.group(1) in pending:
            events.append((pending.pop(resumed.group(1)), True))
    violations: list[str] = []
    loaded = 0
    for raw, executed in events:
        path = os.path.normpath(os.fsdecode(strace_bytes(raw)))
        if path in seen:
            continue
        seen.add(path)
        if not (executed or path.endswith(CODE_SUFFIXES)):
            continue
        if not any(segment in GUARDED_SEGMENTS for segment in Path(path).parts):
            continue
        if os.path.isabs(path) and path.startswith(site_real + os.sep):
            loaded += 1
        else:
            violations.append(path)
    return violations, loaded


def tail(text: str) -> str:
    return text[-OUTPUT_TAIL:]


def run_row(
    row: dict[str, Any],
    probe: dict[str, Any],
    names: dict[str, str],
    phase: str,
    version: str,
    out: Path,
    use_strace: bool,
    site: Path,
) -> dict[str, Any]:
    scratch = Path(names["world"]) / "scratch" / phase / row["id"]
    if scratch.exists():
        shutil.rmtree(scratch)
    (scratch / "tmp").mkdir(parents=True)
    stub_state = scratch / "stub-state"
    stub_state.mkdir()
    shutil.copyfile(
        Path(names["stub_state"]) / "answers.json", stub_state / "answers.json"
    )
    local = dict(names, scratch=str(scratch), stub_state=str(stub_state))
    env = {
        "HOME": local["home"],
        "PATH": f"{local['stubs']}:{local['venv_bin']}:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(scratch / "tmp"),
        "XDG_STATE_HOME": f"{local['home']}/.local/state",
        "XDG_CONFIG_HOME": f"{local['home']}/.config",
        "XDG_CACHE_HOME": f"{local['home']}/.cache",
        "XDG_RUNTIME_DIR": str(scratch / "tmp"),
        "LU_PRODUCT_ROOT": local["product"],
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": local["product"],
        "LEARN_UKRAINIAN_PYTHON": local["py"],
        "GH_TOKEN": "drill-fake-token",
        "GITHUB_TOKEN": "drill-fake-token",
        "DELEGATE_MONITOR_API": local["monitor_url"],
        "DRILL_STUB_STATE": local["stub_state"],
        "DRILL_WORKTREE": local["worktree"],
        "DRILL_SOURCES_URL": local["sources_url"],
        "DRILL_MONITOR_URL": local["monitor_url"],
        "DRILL_VERSION": version,
        "DRILL_BASE_PYTHON": local["base_py"],
        "LU_SCRATCH_ROOT": f"{local['world']}/scratchroot",
        "LU_RUNTIME_TMP_BASE_ROOT": f"{local['world']}/scratchroot",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": f"{local['home']}/.gitconfig",
        "GIT_AUTHOR_NAME": "drill",
        "GIT_AUTHOR_EMAIL": "drill@example.invalid",
        "GIT_COMMITTER_NAME": "drill",
        "GIT_COMMITTER_EMAIL": "drill@example.invalid",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    for key, value in (probe.get("env") or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = expand(value, local)
    argv = [expand(part, local) for part in probe["argv"]]
    cwd = Path(expand(probe.get("cwd", "{scratch}"), local))
    trace = out / "traces" / phase / f"{row['id']}.strace"
    run_argv = argv
    if use_strace:
        trace.parent.mkdir(parents=True, exist_ok=True)
        run_argv = [
            "strace",
            "-f",
            "-qq",
            "-s",
            "4096",
            "-y",
            "-e",
            "trace=open,openat,openat2,execve,execveat",
            "-o",
            str(trace),
            "--",
            *argv,
        ]
    started = time.monotonic()
    started_utc = now_utc()
    timed_out = False
    try:
        result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit - row argv from the kit's rows.json, no shell
            run_argv,
            check=False,
            env=env,
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=probe.get("timeout", 180),
            stdin=subprocess.DEVNULL,
        )
        rc, stdout, stderr = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = None
        stdout = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
    duration = round(time.monotonic() - started, 3)
    expect = probe.get("expect", {})
    failures: list[str] = []
    if timed_out:
        failures.append(f"timed out after {probe.get('timeout', 180)}s")
    wanted_rc = expect.get("rc", 0)
    wanted = wanted_rc if isinstance(wanted_rc, list) else [wanted_rc]
    if rc not in wanted:
        failures.append(f"exit {rc}, expected {wanted}")
    for pattern in expect.get("stdout_re", []):
        if not re.search(expand(pattern, local), stdout, re.MULTILINE):
            failures.append(f"stdout lacks /{pattern}/")
    for pattern in expect.get("output_re", []):
        if not re.search(expand(pattern, local), stdout + stderr, re.MULTILINE):
            failures.append(f"output lacks /{pattern}/")
    for item in expect.get("files", []):
        path = Path(expand(item["path"], local))
        if not path.is_file():
            failures.append(f"missing file {item['path']}")
        elif item.get("re") and not re.search(
            item["re"], path.read_text(errors="replace"), re.MULTILINE
        ):
            failures.append(f"{item['path']} lacks /{item['re']}/")
    origin: dict[str, Any] = {"method": "strace" if use_strace else "none"}
    if use_strace:
        violations, loaded = origin_findings(trace, site)
        origin.update(
            {
                "violations": violations[:20],
                "violation_count": len(violations),
                "release_code_files": loaded,
            }
        )
        if violations:
            failures.append(
                f"{len(violations)} machinery code file(s) loaded from outside the release"
            )
        machinery = expect.get("machinery_loaded")
        if machinery is True and loaded == 0:
            failures.append("row loaded no release code (expected the release to run)")
        if machinery is False and loaded:
            failures.append(
                f"stand-in loaded {loaded} release code file(s) (expected none)"
            )
    else:
        origin["note"] = "strace unavailable or disabled: origin unverified"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "row": row["id"],
        "class": row["class"],
        "kind": row["kind"],
        "probe": row["probe"],
        "mocks": probe.get("mocks", []),
        "phase": phase,
        "version": version,
        "status": "fail" if failures else "pass",
        "failures": failures,
        "exit_code": rc,
        "started_utc": started_utc,
        "duration_s": duration,
        "origin": origin,
        "stdout_tail": tail(stdout),
        "stderr_tail": tail(stderr),
    }
    dest = out / "receipts" / phase / f"{row['id']}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(receipt, indent=1, ensure_ascii=False) + "\n")
    return receipt


def crash_receipt(
    row: dict[str, Any], phase: str, version: str, out: Path, exc: Exception
) -> dict[str, Any]:
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "row": row["id"],
        "class": row["class"],
        "kind": row["kind"],
        "probe": row["probe"],
        "mocks": [],
        "phase": phase,
        "version": version,
        "status": "fail",
        "failures": [f"runner error: {type(exc).__name__}: {exc}"[:500]],
        "exit_code": None,
        "started_utc": now_utc(),
        "duration_s": 0.0,
        "origin": {"method": "none", "note": "row did not complete"},
        "stdout_tail": "",
        "stderr_tail": "",
    }
    dest = out / "receipts" / phase / f"{row['id']}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(receipt, indent=1, ensure_ascii=False) + "\n")
    return receipt


def run_phase(
    data: dict[str, Any],
    names: dict[str, str],
    phase: str,
    version: str,
    out: Path,
    use_strace: bool,
    site: Path,
    only: set[str],
) -> list[dict[str, Any]]:
    """Run every selected row with the Monitor and Sources stand-ins on their default loopback ports."""
    receipts = []
    vesum = frozenset(
        line.split("\t")[0]
        for line in Path(names["fixtures"], "vesum-standin.tsv")
        .read_text()
        .splitlines()
        if line
    )
    stand_ins = out / "stand-ins" / phase
    stand_ins.mkdir(parents=True, exist_ok=True)
    with (
        stub_server(
            SOURCES_PORT, sources_routes(), stand_ins / "sources-requests.jsonl", vesum
        ) as sources_url,
        stub_server(
            MONITOR_PORT, monitor_routes(), stand_ins / "monitor-requests.jsonl", vesum
        ) as monitor_url,
    ):
        local = dict(
            names,
            sources_url=sources_url,
            monitor_url=monitor_url,
            version=version,
            phase=phase,
        )
        for row in data["rows"]:
            if only and row["id"] not in only and row["probe"] not in only:
                continue
            try:
                receipt = run_row(
                    row,
                    data["probes"][row["probe"]],
                    local,
                    phase,
                    version,
                    out,
                    use_strace,
                    site,
                )
            except Exception as exc:  # noqa: BLE001 - a row that crashes the runner is a failed row, never a missing one
                receipt = crash_receipt(row, phase, version, out, exc)
            mark = "PASS" if receipt["status"] == "pass" else "FAIL"
            log(
                f"{phase} {row['id']} {row['probe']} {mark} {receipt['duration_s']}s"
                + ("" if mark == "PASS" else f" {receipt['failures']}")
            )
            receipts.append(receipt)
    return receipts


def seed_world(names: dict[str, str]) -> None:
    """Create the Fleet Comms plane's cto channel in the synthetic product with the installed release.

    The plane persists across rollback, as a real plane does; the rollback rows read what the
    forward release wrote.
    """
    env = base_env(Path(names["home"]))
    env.update(
        {
            "LU_PRODUCT_ROOT": names["product"],
            "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": names["product"],
        }
    )
    run_checked(
        [
            names["py"],
            "-I",
            "-m",
            "lu_machinery.fleet_comms",
            "channel",
            "create",
            "cto",
            "--subscriber",
            "claude-cto",
        ],
        env=env,
        cwd=Path(names["product"]),
        what="seed Fleet Comms cto channel",
        timeout=120,
    )


# ---------------------------------------------------------------------------------------------
# Isolation and orchestration
# ---------------------------------------------------------------------------------------------

SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc")
PRIVATE_ROOTS = ("/tmp", "/var/tmp", "/run")


def bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--unshare-net",
            "--",
            "/bin/true",
        ],  # fixed argv, no shell
        check=False,
        capture_output=True,
    )
    return probe.returncode == 0


def interpreter_roots(base_python: str) -> list[str]:
    """Directories the selected base interpreter needs: its executable's directory and its prefixes."""
    script = "import json, os, sys; print(json.dumps([os.path.realpath(sys.base_prefix), os.path.realpath(sys.base_exec_prefix)]))"
    result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [
            base_python,
            "-I",
            "-c",
            script,
        ],  # the operator-selected interpreter, no shell
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    roots = {
        str(Path(os.path.realpath(base_python)).parent),
        *json.loads(result.stdout),
    }
    return sorted(roots)


def bwrap_prefix(workdir: Path, roots: list[str]) -> list[str]:
    """An allowlisted sandbox: the host is not bound, only what a row needs.

    Read-only: the system directories (``SYSTEM_ROOTS``), the kit and the base interpreter's
    installation. Writable: the work directory alone. Private: ``/proc`` (own PID namespace),
    a minimal ``/dev``, empty ``/tmp``, ``/var/tmp`` and ``/run``, its own network namespace
    (loopback only) and IPC namespace. No home directory, host socket or other checkout is
    reachable from a row.
    """
    argv = [
        "bwrap",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--die-with-parent",
    ]
    argv += ["--proc", "/proc", "--dev", "/dev"]
    for root in SYSTEM_ROOTS:
        path = Path(root)
        if path.is_symlink():
            argv += ["--symlink", os.readlink(root), root]
        elif path.is_dir():
            argv += ["--ro-bind", root, root]
    for root in PRIVATE_ROOTS:
        argv += ["--tmpfs", root]
    keep = {
        str(KIT): "--ro-bind",
        **dict.fromkeys(roots, "--ro-bind"),
        str(workdir): "--bind",
    }
    covered = [
        root
        for root in SYSTEM_ROOTS
        if Path(root).is_dir() and not Path(root).is_symlink()
    ]
    for path, flag in sorted(keep.items(), key=lambda item: len(item[0])):
        if flag == "--ro-bind" and any(
            path == root or path.startswith(root + "/") for root in covered
        ):
            continue
        argv += [flag, path, path]
    return [*argv, "--"]


def resolve_interpreter(choice: str | None) -> str:
    """The real path of the base interpreter for the venv: this one, or ``--python`` found on PATH.

    A bare name is looked up on PATH, never against the current directory, and the interpreter
    itself reports its real base executable, so a venv or a symlink resolves to the installation
    the sandbox must bind.
    """
    if choice is None:
        return base_interpreter()
    found = choice if os.sep in choice else shutil.which(choice)
    if not found or not os.path.isfile(found):
        raise DrillError(
            f"--python {choice!r} is not an interpreter on PATH or an existing file"
        )
    script = (
        "import json, os, sys; print(json.dumps([os.path.realpath(getattr(sys, '_base_executable', None) "
        "or sys.executable), sys.version_info[:2] >= (3, 12)]))"
    )
    result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [os.path.abspath(found), "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        path, new_enough = json.loads(result.stdout)
    except ValueError:
        raise DrillError(
            f"--python {choice!r} did not run: {result.stderr.strip()[-300:]}"
        ) from None
    if not new_enough:
        raise DrillError(f"--python {choice!r} is older than Python 3.12")
    return path


def base_interpreter() -> str:
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def preflight(use_strace: bool, isolation: str) -> dict[str, Any]:
    problems = []
    if sys.version_info < (3, 12):  # noqa: UP036 - the kit runs on whatever python3 a clean machine has
        problems.append(f"python {platform.python_version()} < 3.12")
    if platform.system() != "Linux":
        problems.append(f"platform {platform.system()} is not Linux")
    for tool in ("git", "bash"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} not on PATH")
    if use_strace and not strace_usable():
        problems.append(
            "strace missing or not permitted (install strace, or pass --no-strace for an unverified-origin run)"
        )
    if isolation == "bwrap" and not bwrap_usable():
        problems.append(
            "bwrap missing or cannot create a network namespace (install bubblewrap, or pass --isolation none)"
        )
    leaked = sorted(
        k
        for k in os.environ
        if k.startswith(("LU_", "LEARN_UKRAINIAN_")) or k == "PYTHONPATH"
    )
    if leaked:
        problems.append(
            "environment carries "
            + ", ".join(leaked)
            + " (start from a clean shell or env -i)"
        )
    if isolation == "none" and (KIT.parent.parent / "lu_machinery").is_dir():
        problems.append(
            "the kit sits inside a machinery checkout; copy drill/clean-machine out first (README step 2) or use bwrap isolation"
        )
    if problems:
        raise DrillError("; ".join(problems))
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git": subprocess.run(
            ["git", "--version"], check=False, capture_output=True, text=True
        ).stdout.strip(),
        "strace": use_strace,
        "isolation": isolation,
    }


def summarize(
    phases: dict[str, list[dict[str, Any]]], data: dict[str, Any]
) -> dict[str, Any]:
    summary: dict[str, Any] = {"phases": {}}
    for phase, receipts in phases.items():
        failed = [r["row"] for r in receipts if r["status"] != "pass"]
        summary["phases"][phase] = {
            "version": receipts[0]["version"] if receipts else None,
            "rows": len(receipts),
            "pass": len(receipts) - len(failed),
            "fail": len(failed),
            "failed_rows": failed,
            "seconds": round(sum(r["duration_s"] for r in receipts), 1),
        }
    counts: dict[str, int] = dict.fromkeys(data["classes"], 0)
    for row in data["rows"]:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    summary["classification"] = counts
    return summary


def run_isolated_phase(
    workdir: Path, phase: str, version: str, isolation: str, only: list[str]
) -> list[dict[str, Any]]:
    """Run one phase in a child process (inside bwrap when isolated) and collect its receipts."""
    names = json.loads((workdir / "out" / "names.json").read_text())
    argv = [
        names["base_py"],
        "-I",
        str(KIT / "drill.py"),
        "phase",
        "--workdir",
        str(workdir),
        "--phase",
        phase,
        "--version",
        version,
    ]
    for item in only:
        argv += ["--only", item]
    if isolation == "bwrap":
        argv = bwrap_prefix(workdir, names["interpreter_roots"]) + argv
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": names["home"],
    }
    result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        argv, check=False, env=env, cwd=workdir
    )
    receipts_dir = workdir / "out" / "receipts" / phase
    receipts = [
        json.loads(path.read_text()) for path in sorted(receipts_dir.glob("*.json"))
    ]
    expected = selected_rows(load_rows(KIT / "rows.json"), set(only))
    got = sorted(receipt["row"] for receipt in receipts)
    if got != sorted(expected):
        missing = sorted(set(expected) - set(got))
        raise DrillError(
            f"phase {phase} runner exited {result.returncode} with {len(got)} of {len(expected)} receipts (missing {missing[:10]})"
        )
    wrong = [
        r["row"] for r in receipts if r["phase"] != phase or r["version"] != version
    ]
    if wrong:
        raise DrillError(
            f"phase {phase} receipts name another phase or version: {wrong[:10]}"
        )
    failed = any(receipt["status"] != "pass" for receipt in receipts)
    if result.returncode != (1 if failed else 0):
        raise DrillError(
            f"phase {phase} runner exited {result.returncode}, inconsistent with its receipts"
        )
    return receipts


def selected_rows(data: dict[str, Any], only: set[str]) -> list[str]:
    return [
        row["id"]
        for row in data["rows"]
        if not only or row["id"] in only or row["probe"] in only
    ]


def phase_main(args: argparse.Namespace) -> int:
    """Internal: run one phase in this process (the parent starts it, isolated)."""
    workdir = args.workdir.resolve()
    out = workdir / "out"
    names = json.loads((out / "names.json").read_text())
    data = load_rows(KIT / "rows.json")
    receipts = run_phase(
        data,
        names,
        args.phase,
        args.version,
        out,
        names["strace"] == "1",
        Path(names["site"]),
        set(args.only),
    )
    return 0 if all(r["status"] == "pass" for r in receipts) else 1


COMPACT_LIMIT = 15 * 1024


def _failure_line(receipt: dict[str, Any]) -> str:
    detail = ""
    for line in reversed(receipt["stdout_tail"].splitlines()):
        if line.startswith('{"probe"'):
            try:
                detail = json.dumps(json.loads(line).get("detail"), ensure_ascii=False)
            except ValueError:
                detail = line
            break
    reasons = "; ".join(receipt["failures"])
    return f"  {receipt['row']} {receipt['probe']}: {reasons} {detail[:300]}".rstrip()


def compact_summary(
    summary: dict[str, Any], phases: dict[str, list[dict[str, Any]]]
) -> str:
    """Plain-text summary for a reply limited to 16 KiB; the receipts keep every detail."""
    lines = [
        f"lu-machinery clean-machine drill: {summary['result'].upper()} at {summary['finished_utc']}"
    ]
    for phase, info in summary["phases"].items():
        lines.append(
            f"{phase} v{info['version']}: {info['pass']}/{info['rows']} rows pass in {info['seconds']} s"
        )
        lines += [
            _failure_line(r) for r in phases.get(phase, []) if r["status"] != "pass"
        ]
    checks = ", ".join(
        f"{name} {'ok' if item.get('ok') else 'FAIL'} ({item.get('checked', 0)} files, {item.get('problem_count', '?')} problems)"
        for name, item in summary["integrity"].items()
    )
    lines.append(f"RECORD checks: {checks}")
    receipts = [r for rows in phases.values() for r in rows]
    violations = sum(r["origin"].get("violation_count", 0) for r in receipts)
    lines.append(
        f"origin: {summary['environment']['strace'] and 'strace' or 'not checked'}, {violations} violations; isolation: {summary['environment']['isolation']}"
    )
    lines.append(
        "classes: "
        + ", ".join(f"{k} {v}" for k, v in sorted(summary["classification"].items()))
    )
    lines.append(
        "wheels sha256: " + ", ".join(f"v{v} {h}" for v, h in summary["wheels"].items())
    )
    env = summary["environment"]
    lines.append(
        f"environment: python {env['python']}, {env['platform']}, {env['git']}"
    )
    if summary.get("partial"):
        lines.append("partial run (--only or --forward-only): not a drill result")
    text = "\n".join(lines) + "\n"
    if len(text.encode()) > COMPACT_LIMIT:
        text = (
            text.encode()[: COMPACT_LIMIT - 64].decode(errors="ignore")
            + "\n[truncated; see out/summary.json]\n"
        )
    return text


def clean_workdir(workdir: Path, phases: dict[str, list[dict[str, Any]]]) -> None:
    """Delete everything the drill built except out/, and the traces of rows that passed."""
    for name in ("venv", "world", "wheels"):
        shutil.rmtree(workdir / name, ignore_errors=True)
    for phase, receipts in phases.items():
        for receipt in receipts:
            if receipt["status"] == "pass":
                (
                    workdir / "out" / "traces" / phase / f"{receipt['row']}.strace"
                ).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        choices=["all", "phase"],
        help="all: setup, run, rollback, rerun, roll-forward (phase is internal)",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        help="Empty or drill-owned directory for every artifact (default: a new ./lu-drill-<UTC time>)",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Keep the venv, mock world, wheels and every trace (default: keep only out/)",
    )
    parser.add_argument(
        "--version", default="0.1.28", help="Forward release (default 0.1.28)"
    )
    parser.add_argument(
        "--rollback-to", default="0.1.27", help="Previous release (default 0.1.27)"
    )
    parser.add_argument(
        "--wheel-dir",
        type=Path,
        help="Offline: directory with v<version>/<wheel> and SHA256SUMS",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Base interpreter for the venv (default: this one, resolved)",
    )
    parser.add_argument(
        "--no-strace",
        action="store_true",
        help="Skip the origin trace (receipts say origin unverified)",
    )
    parser.add_argument(
        "--isolation",
        choices=["auto", "bwrap", "none"],
        default="auto",
        help="Run rows in a bwrap sandbox: own network namespace, empty /home and /tmp (auto: when bwrap works)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Run only this row id or probe id (repeatable)",
    )
    parser.add_argument(
        "--forward-only",
        action="store_true",
        help="Run the forward phase only (no rollback); a partial drill",
    )
    parser.add_argument(
        "--reuse-venv",
        action="store_true",
        help="Keep an existing workdir venv instead of rebuilding it",
    )
    parser.add_argument("--phase", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "phase":
        try:
            return phase_main(args)
        except DrillError as exc:
            print(f"drill: phase failed: {exc}", file=sys.stderr)
            return 2

    if args.workdir is None:
        args.workdir = (
            Path.cwd() / f"lu-drill-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        )
        if args.workdir.exists():
            print(f"drill: {args.workdir} already exists", file=sys.stderr)
            return 2
    workdir = args.workdir.resolve()
    use_strace = not args.no_strace
    isolation = args.isolation
    if isolation == "auto":
        isolation = "bwrap" if bwrap_usable() else "none"
    integrity: dict[str, Any] = {}
    phases: dict[str, list[dict[str, Any]]] = {}
    try:
        base_py = resolve_interpreter(args.python)
        env_report = preflight(use_strace, isolation)
        workdir.mkdir(parents=True, exist_ok=True)
        marker = workdir / ".lu-clean-machine-drill"
        if any(workdir.iterdir()) and not marker.exists():
            raise DrillError(f"{workdir} is not empty and not a drill workdir")
        marker.write_text("lu-machinery clean-machine drill workdir\n")
        out = workdir / "out"
        if out.exists():
            shutil.rmtree(out)
        out.mkdir()
        data = load_rows(KIT / "rows.json")
        offline = args.wheel_dir.resolve() if args.wheel_dir else None
        wheels = workdir / "wheels"
        forward = obtain_wheel(args.version, wheels, offline)
        previous = obtain_wheel(args.rollback_to, wheels, offline)
        venv = workdir / "venv"
        if venv.exists() and not args.reuse_venv:
            shutil.rmtree(venv)
        paths = build_world(workdir, base_py)
        home = paths["home"]
        py = venv / "bin" / "python"
        if not py.exists():
            log(f"setup: venv from {base_py}")
            py = create_venv(base_py, venv, home)
        log(f"setup: install v{args.version}")
        install_wheel(py, forward, home)
        site = Path(
            run_checked(
                [
                    str(py),
                    "-I",
                    "-c",
                    "import sysconfig;print(sysconfig.get_paths()['purelib'])",
                ],
                env=base_env(home),
                cwd=home,
                what="site",
            ).strip()
        )
        integrity["setup"] = check_integrity(py, args.version, home)
        if not integrity["setup"]["ok"]:
            raise DrillError(
                f"installed v{args.version} failed integrity: {integrity['setup']}"
            )
        names = {
            "py": str(py),
            "venv_bin": str(py.parent),
            "site": str(site),
            "kit": str(KIT),
            "base_py": base_py,
            "world": str(paths["world"]),
            "home": str(home),
            "product": str(paths["product"]),
            "worktree": str(paths["worktree"]),
            "remote": str(paths["remote"]),
            "stubs": str(paths["stubs"]),
            "stub_state": str(paths["stubs"].parent / "state"),
            "fixtures": str(paths["world"] / "fixtures"),
            "strace": "1" if use_strace else "0",
            "isolation": isolation,
            "interpreter_roots": interpreter_roots(base_py),
        }
        (out / "names.json").write_text(json.dumps(names, indent=1))
        seed_world(names)
        phases["forward"] = run_isolated_phase(
            workdir, "forward", args.version, isolation, args.only
        )
        if not args.forward_only:
            log(f"rollback: install v{args.rollback_to}")
            install_wheel(py, previous, home)
            integrity["rollback"] = check_integrity(py, args.rollback_to, home)
            phases["rollback"] = run_isolated_phase(
                workdir, "rollback", args.rollback_to, isolation, args.only
            )
            log(f"roll-forward: install v{args.version}")
            install_wheel(py, forward, home)
            integrity["roll_forward"] = check_integrity(py, args.version, home)
    except (
        DrillError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        OSError,
    ) as exc:
        print(f"drill: setup failed: {exc}", file=sys.stderr)
        return 2
    summary = summarize(phases, data)
    summary.update(
        {
            "schema": SUMMARY_SCHEMA,
            "finished_utc": now_utc(),
            "forward_version": args.version,
            "rollback_version": args.rollback_to,
            "partial": bool(args.forward_only or args.only),
            "wheels": {
                v: sha256_file(w)
                for v, w in ((args.version, forward), (args.rollback_to, previous))
            },
            "integrity": integrity,
            "environment": env_report,
        }
    )
    ok = (
        not summary["partial"]
        and all(p["fail"] == 0 for p in summary["phases"].values())
        and all(i.get("ok") for i in integrity.values())
    )
    summary["result"] = "pass" if ok else ("partial" if summary["partial"] else "fail")
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    compact = compact_summary(summary, phases)
    (out / "summary.txt").write_text(compact)
    if not args.keep_work:
        clean_workdir(workdir, phases)
    print(compact, end="")
    log(
        f"receipts in {out / 'receipts'}; summary {out / 'summary.json'} and {out / 'summary.txt'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
