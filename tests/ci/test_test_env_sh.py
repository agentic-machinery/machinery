"""Behavior tests for scripts/ci/test_env.sh (#9450)."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

from lu_machinery.common.repo_root import machinery_path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = machinery_path('ci/test_env.sh')
def _seed(tmp_path: Path, tasks: dict[str, tuple[str | None, str]]) -> Path:
    """Create the state `start` leaves behind: tasks, logs and optional rc files."""
    env_dir = tmp_path / "test-env"
    env_dir.mkdir()
    (env_dir / "tasks").write_text("".join(f"{name}\n" for name in tasks))
    for name, (rc, log) in tasks.items():
        (env_dir / f"{name}.log").write_text(log)
        if rc is not None:
            (env_dir / f"{name}.rc").write_text(f"{rc}\n")
    return env_dir


def _wait(tmp_path: Path, limit: str | None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path)}
    env.pop("TEST_ENV_WAIT_TIMEOUT_S", None)
    if limit is not None:
        env["TEST_ENV_WAIT_TIMEOUT_S"] = limit
    return subprocess.run(
        ["bash", str(SCRIPT), "wait"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_missing_marker_fails_within_limit_and_names_task(tmp_path: Path) -> None:
    _seed(tmp_path, {"postgres": (None, "starting postgres\n")})
    started = time.monotonic()
    result = _wait(tmp_path, "2")
    elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert elapsed < 10
    assert "::error::postgres setup did not finish within 2s" in result.stdout
    assert "starting postgres" in result.stdout


def test_empty_marker_is_not_finished_yet(tmp_path: Path) -> None:
    env_dir = _seed(tmp_path, {"postgres": (None, "pg log\n")})
    (env_dir / "postgres.rc").write_text("")
    result = _wait(tmp_path, "2")
    assert result.returncode == 1
    assert "exit )" not in result.stdout
    assert "::error::postgres setup did not finish within 2s" in result.stdout


def test_start_publishes_marker_atomically(tmp_path: Path) -> None:
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path)}
    started = subprocess.run(
        ["bash", str(SCRIPT), "start", "bogus"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert started.returncode == 0
    result = _wait(tmp_path, "20")
    assert result.returncode == 1
    assert "::group::bogus setup (exit 2)" in result.stdout
    assert "unknown task: bogus" in result.stdout
    assert not (tmp_path / "test-env" / "bogus.rc.tmp").exists()


def test_marker_is_never_visible_while_its_write_is_in_flight(tmp_path: Path) -> None:
    """Stall the writer between creating the temp file and publishing it."""
    # `mv` is the publish step; a stand-in that sleeps first holds the writer in
    # the window where `.rc.tmp` exists but `.rc` must not.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    slow_mv = fake_bin / "mv"
    release = tmp_path / "release-mv"
    slow_mv.write_text(
        f'#!/bin/sh\nwhile [ ! -e "{release}" ]; do sleep 0.01; done\nexec /bin/mv "$@"\n'
    )
    slow_mv.chmod(slow_mv.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    started = subprocess.run(
        ["bash", str(SCRIPT), "start", "bogus"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert started.returncode == 0
    env_dir = tmp_path / "test-env"
    marker, temp = env_dir / "bogus.rc", env_dir / "bogus.rc.tmp"
    deadline = time.monotonic() + 60
    while not temp.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    # The writer is now stalled in `mv`: nothing may be published yet.
    try:
        assert temp.exists(), "temp marker was never created"
        assert not marker.exists(), "marker visible before it was published"
    finally:
        release.touch()
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.read_text().strip() == "2"
    assert not temp.exists()


def test_finished_tasks_success_unchanged(tmp_path: Path) -> None:
    _seed(tmp_path, {"postgres": ("0", "pg ok\n"), "npm": ("0", "npm ok\n")})
    result = _wait(tmp_path, None)
    assert result.returncode == 0
    assert "::group::postgres setup (exit 0)\npg ok\n::endgroup::" in result.stdout
    assert "::group::npm setup (exit 0)\nnpm ok\n::endgroup::" in result.stdout
    assert "::error::" not in result.stdout


def test_finished_task_failure_unchanged(tmp_path: Path) -> None:
    _seed(tmp_path, {"postgres": ("0", "pg ok\n"), "npm": ("1", "npm boom\n")})
    result = _wait(tmp_path, None)
    assert result.returncode == 1
    assert "::group::npm setup (exit 1)\nnpm boom\n::endgroup::" in result.stdout
    assert "::error::npm setup failed (exit 1); see its log group above" in result.stdout
    assert "did not finish" not in result.stdout


def test_postgres_start_resolves_sibling_from_foreign_cwd(tmp_path: Path) -> None:
    """Installed shell resources must find siblings outside the caller's checkout."""
    resources = tmp_path / "installed resources"
    resources.mkdir()
    script = resources / "test_env.sh"
    script.write_bytes(SCRIPT.read_bytes())
    (resources / "start_postgres.sh").write_text('echo "sibling postgres started"\n')
    caller = tmp_path / "product"
    caller.mkdir()
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path)}
    started = subprocess.run(
        ["bash", str(script), "start", "postgres"], cwd=caller, env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert started.returncode == 0
    result = subprocess.run(
        ["bash", str(script), "wait"], cwd=caller, env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sibling postgres started" in result.stdout


def test_cache_lock_progress_does_not_mask_later_mirror_failure(tmp_path: Path) -> None:
    """A past cache-lock wait must not suppress fallback for a later fetch error."""
    setup = machinery_path("ci/setup_bubblewrap.sh").read_text()
    # Only the classifier is evaluated. The host-changing setup body never runs.
    classifier = setup.split("apt_failure_is_dpkg_lock() {", 1)[1].split("\n}\n", 1)[0]
    log = tmp_path / "apt.log"
    log.write_text(
        "Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend. "
        "It is held by process 123 (apt-get)\n"
        "E: Failed to fetch https://mirror.invalid/package.deb Temporary failure resolving\n"
    )
    result = subprocess.run(
        ["bash", "-c", "apt_failure_is_dpkg_lock() {" + classifier + "\n}\n"
         'apt_failure_is_dpkg_lock 100 "$1"', "_", str(log)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_snapshot_sources_restrict_components_to_main_and_preserve_integrity() -> None:
    """Defect class: pulling unneeded archive components or regressing snapshot integrity/retries."""
    setup = machinery_path("ci/setup_bubblewrap.sh").read_text()
    sources_block = setup.split('cat > "$apt_dir/ubuntu.sources" <<EOF\n', 1)[1].split(
        "\nEOF\n", 1
    )[0]
    sources = dict(line.split(": ", 1) for line in sources_block.splitlines())
    assert sources["Components"] == "main"
    assert sources["Suites"] == "noble noble-updates noble-security"
    assert sources["Signed-By"] == "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
    assert sources["Snapshot"] == "no"
    assert "-o Acquire::Retries=3" in setup
    assert "-o Acquire::Retries=1" not in setup
    assert "--error-on=any" in setup
    download = setup.split("if ! timeout ", 1)[1].split("; then", 1)[0]
    assert download.startswith("--kill-after=1 29 ")
    assert "--max-time 15" in download
    assert "--retry 3" in download
    checksum = 'echo "${digest}  $apt_dir/$deb" | sha256sum -c -'
    assert setup.index(download) < setup.index(checksum) < setup.index('missing+=("$apt_dir/$deb")')


def test_snapshot_update_timeout_derives_from_apt_deadline_and_fails_closed() -> None:
    """Defect class: hardcoded 60 s update cap, budget overrun past apt_deadline, or timeout 0."""
    setup = machinery_path("ci/setup_bubblewrap.sh").read_text()
    assert "apt_deadline=$((SECONDS + 195))" in setup
    assert 'apt_install 55 40 "${missing[@]}"' in setup
    update_block = setup.split("apt_deadline=$((SECONDS + 195))\n", 1)[1].split(
        '\n  install_log="$apt_dir/install.log"', 1
    )[0]
    assert 'timeout --kill-after=5 "$update_cap"' in update_block
    assert "--error-on=any" in update_block

    # Only the extracted arithmetic/guard block is evaluated with a shell-function
    # `sudo` stub; the host-changing setup script and real apt never run.
    def _eval_update_block(deadline_offset: int) -> subprocess.CompletedProcess[str]:
        script = (
            "set -euo pipefail\n"
            "snapshot=20261122T000000Z\n"
            "apt_opts=(-o Acquire::Retries=3)\n"
            'sudo() { printf "SUDO:%s\\n" "$*"; return 0; }\n'
            f"apt_deadline=$((SECONDS + {deadline_offset}))\n"
            f"{update_block}\n"
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    fresh = _eval_update_block(195)
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert fresh.stdout.startswith("SUDO:timeout --kill-after=5 "), fresh.stdout
    tokens = fresh.stdout.strip().split()
    cap = int(tokens[2])
    assert 129 <= cap <= 130, f"expected derived cap in 129..130, got {cap}"
    # Update cap + 5 s kill grace + first apt_install (55 s + 5 s kill grace) <= 195 s.
    assert cap + 5 + 55 + 5 <= 195

    for exhausted_offset in (65, 10):
        exhausted = _eval_update_block(exhausted_offset)
        assert exhausted.returncode == 1, exhausted.stdout + exhausted.stderr
        assert "SUDO:" not in exhausted.stdout
        assert "::error::Could not update package indexes" in exhausted.stderr
