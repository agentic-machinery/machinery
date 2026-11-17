"""Process identity and liveness probes shared by lease fencing and review isolation.

A lease records who owns it (machine id, owner pid, process start time) and a
later reader decides whether that owner is still alive. Thread handoff lease
fencing and review isolation both make that decision, so the probes live here
rather than inside the rollover CLI.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lu_machinery.common import github_client


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(
    args: list[str],
    *,
    cwd: Path,
    timeout_s: int = 10,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    try:
        completed = github_client.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        return CommandResult(returncode=127, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return CommandResult(
            returncode=124,
            stdout=str(stdout).strip(),
            stderr=(str(stderr).strip() or f"timeout after {timeout_s}s"),
        )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def git_environment() -> dict[str, str]:
    """Keep inherited hook state from redirecting Git away from ``cwd``."""
    redirecting_variables = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_WORK_TREE",
    }
    return {key: value for key, value in os.environ.items() if key not in redirecting_variables}


@dataclass(frozen=True)
class ProcessSnapshot:
    """One process's identity facts, gathered atomically from a single probe.

    ``candidate_basenames`` deliberately holds more than one name: measured on
    a real Claude Code session, ``psutil.Process.name()`` for the ``claude``
    harness process returns its version string (e.g. ``"2.1.220"``), not
    ``claude`` — the CLI overrides its own process title. ``cmdline()[0]`` and
    ``ps -o comm=`` both still resolve to a path basename of ``claude``.
    Matching against every candidate we can cheaply gather is what makes the
    harness-ancestor walk work on the machine it actually has to run on,
    rather than only in a clean test environment.
    """

    pid: int
    ppid: int | None
    candidate_basenames: frozenset[str]
    started_at: float | None


def _parse_ps_lstart(raw: str) -> float | None:
    """Parse ``ps -o lstart=`` (whole-second, local-time) into epoch seconds."""
    try:
        parsed = datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.timestamp()


def _default_process_snapshot(pid: int) -> ProcessSnapshot | None:
    """Probe one live process for ppid, candidate executable names, and start time.

    Prefers psutil (locked in requirements-lock.txt) for one atomic read.
    Falls back to ``ps`` (forced ``LC_ALL=C`` so ``lstart`` parses regardless
    of locale) when psutil is unavailable or cannot see the process.
    """
    try:
        import psutil

        try:
            proc = psutil.Process(pid)
            candidates: set[str] = set()
            name = proc.name()
            if name:
                candidates.add(Path(name).name)
            for accessor in (proc.exe, proc.cmdline):
                try:
                    value = accessor()
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    continue
                if isinstance(value, list):
                    if value and value[0]:
                        candidates.add(Path(value[0]).name)
                elif value:
                    candidates.add(Path(value).name)
            return ProcessSnapshot(
                pid=pid,
                ppid=proc.ppid(),
                candidate_basenames=frozenset(candidates),
                started_at=proc.create_time(),
            )
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied:
            pass  # fall through to the ps fallback, which may still resolve lstart
    except ImportError:
        pass

    env = git_environment()
    env["LC_ALL"] = "C"
    result = run_command(
        ["ps", "-o", "ppid=,comm=,lstart=", "-p", str(pid)],
        cwd=Path.cwd(),
        env=env,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    fields = result.stdout.strip().split(None, 2)
    if len(fields) < 3:
        return None
    try:
        ppid = int(fields[0])
    except ValueError:
        ppid = None
    candidate_basenames = frozenset({Path(fields[1]).name}) if fields[1] else frozenset()
    started_at = _parse_ps_lstart(fields[2])
    return ProcessSnapshot(pid=pid, ppid=ppid, candidate_basenames=candidate_basenames, started_at=started_at)


def _default_machine_id() -> str | None:
    """Return a stable machine identifier — never a network hostname.

    Hostnames change under macOS mDNS renames, VPN reassociation, and
    container rebuilds; keying liveness off one would silently and
    permanently push every lease onto the cross-machine fallback path and
    resurrect the exact lockout this design fixes.
    """
    if sys.platform == "darwin":
        result = run_command(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            cwd=Path.cwd(),
            env=git_environment(),
        )
        if result.returncode == 0:
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
            if match:
                return match.group(1)
    else:
        machine_id_path = Path("/etc/machine-id")
        try:
            value = machine_id_path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            return value
    hostname = socket.gethostname().strip()
    return hostname or None


def _process_is_alive(pid: int) -> bool:
    """POSIX liveness probe. EPERM means the process exists (owned by another user).

    A zombie (defunct) process also answers `kill(pid, 0)` successfully — its pid slot is
    reserved until its parent reaps it — but it can never run again, refresh a heartbeat, or
    release its lease. Treating it as alive would conflict with every future claim forever,
    recreating the original lockout (a dead owner blocking every restart) under a new name.
    A confirmed zombie is dead here, not merely absent.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _process_is_zombie(pid)


def _process_is_zombie(pid: int) -> bool:
    """Best-effort zombie probe. Never raises; an inconclusive read means "not confirmed zombie".

    Prefers psutil's structured status. Falls back to `ps -o stat=` (forced `LC_ALL=C`,
    matching `_default_process_snapshot`) when psutil is unavailable — the leading `Z` in the
    state column is portable across macOS and Linux `ps` implementations.
    """
    try:
        import psutil

        try:
            return bool(psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False
    except ImportError:
        pass
    env = git_environment()
    env["LC_ALL"] = "C"
    result = run_command(["ps", "-o", "stat=", "-p", str(pid)], cwd=Path.cwd(), env=env)
    if result.returncode != 0:
        return False
    return result.stdout.strip()[:1] == "Z"
