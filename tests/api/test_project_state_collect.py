"""Unit tests for offline project-state collection helpers (#7188)."""

from __future__ import annotations

import subprocess
from pathlib import Path
import pytest
from lu_machinery.api import project_state_collect as collect_mod
from lu_machinery.api.project_state_collect import SERVICE_DEFINITIONS, classify_serving_root, collect_local_document, collect_primary_state, collect_service_row, resolve_primary_repo_root
from lu_machinery.api.project_state_sanitize import validate_report_document
from lu_machinery.common.release_layout import MANIFEST_NAME
import io
import json
import os
import signal
import socket
import sys
import tarfile
import time
import urllib.error
from lu_machinery.api import release_snapshot
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.common.release_layout import is_release_root
from lu_machinery.path_safety import safe_join
from tests.helpers.python import project_python as project_python
from tests.helpers.product_tree import configured_roots as configured_roots

SHA_MAIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_HEAD = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://github.com/learn-ukrainian/learn-ukrainian.github.io.git",
    )
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    (repo / ".git" / "FETCH_HEAD").write_text("", encoding="utf-8")
    return repo


def test_collect_primary_state_fixture_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    state = collect_primary_state(repo)
    assert state is not None
    assert len(state["head_sha"]) == 40
    assert state["origin_main_sha"] == state["head_sha"]
    assert state["dirty_count"] == 0


def test_collect_primary_dirty_count(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    state = collect_primary_state(repo)
    assert state is not None
    assert state["dirty_count"] == 1


def test_classify_release_root(tmp_path: Path) -> None:
    release = tmp_path / ".runtime" / "api" / "releases" / SHA_MAIN
    release.mkdir(parents=True)
    (release / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    classified = classify_serving_root(release)
    assert classified["serving_mode"] == "release"
    assert classified["serving_sha"] == SHA_MAIN


def test_classify_checkout_root(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    classified = classify_serving_root(repo)
    assert classified["serving_mode"] == "checkout"
    assert classified["checkout_sha"] is not None


def test_resolve_primary_from_dispatch_worktree(tmp_path: Path) -> None:
    primary = _init_repo(tmp_path)
    worktree = tmp_path / "dispatch-worktree"
    _git(primary, "worktree", "add", str(worktree), "-b", "feature/test")
    resolved = resolve_primary_repo_root(worktree)
    assert resolved.resolve() == primary.resolve()


def write_fake_lsof(tmp_path: Path, *, listener_pid: int | None = None, cwd: Path | None = None) -> Path:
    """Stand-in for ``SVC_LSOF_BIN`` so collection never probes the host's live services.

    With no ``listener_pid`` every port reports no listener. Otherwise the api port
    (8765) reports ``listener_pid`` and ``lsof -p`` reports ``cwd`` (the non-/proc path).
    """
    script = tmp_path / "fake-lsof"
    lines = ["#!/bin/sh"]
    if listener_pid is not None:
        lines += [
            f'if [ "$1" = "-tiTCP:8765" ]; then echo {listener_pid}; exit 0; fi',
            f'if [ "$1" = "-p" ]; then echo "p{listener_pid}"; echo "n{cwd}"; exit 0; fi',
        ]
    lines.append("exit 1")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _document_with(row: dict[str, object]) -> dict[str, object]:
    return {
        "host_id": "mac-operator",
        "primary": {
            "head_sha": SHA_HEAD,
            "origin_main_sha": SHA_MAIN,
            "origin_main_age_s": 1.0,
            "ahead": 0,
            "behind": 0,
            "dirty_count": 0,
        },
        "worktrees": {"count": 1},
        "services": [row],
        "collected_at": "2026-09-23T00:00:00Z",
    }


def _running(name: str) -> str:
    return "running"


API = next(item for item in SERVICE_DEFINITIONS if item.name == "api")


def test_collect_service_row_default_state_fn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVC_LSOF_BIN", str(write_fake_lsof(tmp_path)))
    row = collect_service_row(SERVICE_DEFINITIONS[0])
    assert row["name"] == "sources"
    assert row["state"] == "stopped"
    assert row["serving_mode"] == "checkout"
    assert "unresolved_reason" not in row
    validate_report_document(_document_with(row))


def test_running_service_without_listener_pid_is_unresolved() -> None:
    row = collect_service_row(API, service_state=_running, listener_pid=lambda name: None)
    assert row["serving_mode"] == "unknown"
    assert row["unresolved_reason"] == "no_listener_pid"
    assert row["serving_sha"] is None and row["checkout_sha"] is None
    validate_report_document(_document_with(row))


def test_running_service_with_unreadable_cwd_is_unresolved() -> None:
    row = collect_service_row(
        API,
        service_state=_running,
        listener_pid=lambda name: 4242,
        process_cwd=lambda pid: None,
    )
    assert row["serving_mode"] == "unknown"
    assert row["unresolved_reason"] == "cwd_unreadable"
    validate_report_document(_document_with(row))


def test_running_service_with_cwd_outside_git_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub git so the verdict never depends on whether tmp_path sits inside a repo.
    monkeypatch.setattr(collect_mod, "_git", lambda cwd, *args: None)
    row = collect_service_row(
        API,
        service_state=_running,
        listener_pid=lambda name: 4242,
        process_cwd=lambda pid: tmp_path,
    )
    assert row["serving_mode"] == "unknown"
    assert row["unresolved_reason"] == "cwd_not_git_repo"
    validate_report_document(_document_with(row))


def test_running_service_with_unborn_head_is_unresolved(tmp_path: Path) -> None:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-b", "main")
    row = collect_service_row(
        API,
        service_state=_running,
        listener_pid=lambda name: 4242,
        process_cwd=lambda pid: unborn,
    )
    assert row["serving_mode"] == "unknown"
    assert row["unresolved_reason"] == "head_unresolvable"
    validate_report_document(_document_with(row))


def test_running_service_in_checkout_resolves_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    row = collect_service_row(
        API,
        service_state=_running,
        listener_pid=lambda name: 4242,
        process_cwd=lambda pid: repo,
    )
    assert row["serving_mode"] == "checkout"
    assert row["checkout_sha"] is not None
    assert "unresolved_reason" not in row
    validate_report_document(_document_with(row))


def test_collect_local_document_fixture_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    document = collect_local_document(
        "mac-operator",
        repo_root=repo,
        service_state=lambda name: "stopped",
    )
    assert document is not None
    assert document["host_id"] == "mac-operator"
    assert document["primary"]["head_sha"]
    assert len(document["services"]) == 4


VENV_PYTHON = project_python()


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=True,
        env=sanitized_git_env(),
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _create_snapshot_repo(tmp_path: Path, *, api_app: bool = False) -> tuple[Path, str]:
    repo_root = tmp_path / "live"
    (repo_root / "scripts" / "api").mkdir(parents=True)
    (repo_root / "schemas").mkdir()
    (repo_root / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (repo_root / "scripts" / "api" / "__init__.py").write_text("", encoding="utf-8")
    (repo_root / "schemas" / "example.json").write_text("{}\n", encoding="utf-8")
    if api_app:
        (repo_root / "scripts" / "api" / "marker.py").write_text(
            "def value() -> str:\n    return 'snapshot'\n",
            encoding="utf-8",
        )
        (repo_root / "scripts" / "api" / "main.py").write_text(
            "from fastapi import FastAPI\n"
            "from .marker import value\n"
            "app = FastAPI()\n"
            "@app.get('/value')\n"
            "def get_value():\n"
            "    return {'value': value()}\n",
            encoding="utf-8",
        )
    else:
        (repo_root / "scripts" / "api" / "marker.py").write_text("VALUE = 'one'\n", encoding="utf-8")

    for relative_name in release_snapshot.LIVE_DATA_PATHS:
        (repo_root / relative_name).mkdir(parents=True, exist_ok=True)

    _run_git(repo_root.parent, "init", repo_root.name)
    _run_git(repo_root, "config", "user.email", "test@example.invalid")
    _run_git(repo_root, "config", "user.name", "Release Snapshot Test")
    _run_git(repo_root, "add", "scripts", "schemas")
    _run_git(repo_root, "commit", "-m", "initial snapshot")
    return repo_root, _run_git(repo_root, "rev-parse", "HEAD")


def _next_sha(repo_root: Path) -> str:
    marker = repo_root / "scripts" / "api" / "marker.py"
    marker.write_text("VALUE = 'two'\n", encoding="utf-8")
    _run_git(repo_root, "add", str(marker.relative_to(repo_root)))
    _run_git(repo_root, "commit", "-m", "next snapshot")
    return _run_git(repo_root, "rev-parse", "HEAD")


@pytest.fixture
def release_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield listener


def _read_json(url: str, *, timeout: float = 0.5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_atomic_publish_keeps_previous_current_on_crash(tmp_path: Path) -> None:
    repo_root, old_sha = _create_snapshot_repo(tmp_path)
    old_release, _ = release_snapshot.build_release(repo_root, old_sha)
    new_sha = _next_sha(repo_root)

    class SimulatedCrash(BaseException):
        pass

    def crash_before_publish(_staging: Path) -> None:
        raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        release_snapshot.build_release(repo_root, new_sha, before_publish=crash_before_publish)

    release_root = release_snapshot.releases_root(repo_root)
    assert (release_root / "current").resolve() == old_release
    assert (release_root / f".staging-{os.getpid()}").is_dir()
    assert not (release_root / new_sha).exists()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_release_reuse_is_idempotent_and_links_live_data(tmp_path: Path) -> None:
    repo_root, sha = _create_snapshot_repo(tmp_path)
    first_release, reused = release_snapshot.build_release(repo_root, sha)
    second_release, reused_again = release_snapshot.build_release(repo_root, sha)

    assert not reused
    assert reused_again
    assert second_release == first_release
    assert (first_release / "curriculum").is_symlink()
    assert (first_release / "tests" / "fixtures").is_symlink()
    assert [
        path.name
        for path in release_snapshot.releases_root(repo_root).iterdir()
        if path.is_dir() and not path.is_symlink()
    ] == [sha]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_corrupt_release_dir_is_rebuilt_not_reused(tmp_path: Path) -> None:
    """A dirty final release that fails verify must be deleted and rebuilt."""
    repo_root, sha = _create_snapshot_repo(tmp_path)
    first_release, reused = release_snapshot.build_release(repo_root, sha)
    assert not reused

    manifest_path = first_release / MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["file_count"] = int(payload["file_count"]) + 1
    manifest_path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(release_snapshot.ReleaseSnapshotError, match="verification failed"):
        release_snapshot.verify_release(first_release, sha)

    rebuilt, reused_again = release_snapshot.build_release(repo_root, sha)

    assert not reused_again
    assert rebuilt == first_release
    assert release_snapshot.verify_release(rebuilt, sha).sha == sha
    assert (release_snapshot.releases_root(repo_root) / "current").resolve() == rebuilt


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_running_release_keeps_serving_archived_code_after_checkout_mutation(tmp_path: Path, release_listener) -> None:
    repo_root, sha = _create_snapshot_repo(tmp_path, api_app=True)
    release_dir, _ = release_snapshot.build_release(repo_root, sha)
    listener = release_listener
    port = listener.getsockname()[1]
    environment = sanitized_git_env() | {
        "LEARN_UK_REPO_ROOT": str(repo_root),
        "GIT_DIR": str(repo_root / ".git"),
        "GIT_WORK_TREE": str(repo_root),
        "PYTHONPATH": str(release_dir),
    }
    process = subprocess.Popen(
        [
            str(VENV_PYTHON),
            "-B",
            "-m",
            "uvicorn",
            "scripts.api.main:app",
            "--host",
            "127.0.0.1",
            "--fd",
            str(listener.fileno()),
        ],
        pass_fds=(listener.fileno(),),
        cwd=release_dir,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    listener.close()
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                if _read_json(f"http://127.0.0.1:{port}/value") == {"value": "snapshot"}:
                    break
            except (urllib.error.URLError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                pytest.fail("release API did not become ready")
            time.sleep(0.05)

        (repo_root / "scripts" / "api" / "marker.py").write_text(
            "def value() -> str:\n    return 'mutated checkout'\n",
            encoding="utf-8",
        )
        assert _read_json(f"http://127.0.0.1:{port}/value") == {"value": "snapshot"}
    finally:
        process.terminate()
        process.wait(timeout=5)

    assert release_snapshot.verify_release(release_dir, sha).sha == sha
    assert not list((release_dir / "scripts").rglob("*.pyc"))


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_git_service_environment_targets_live_checkout(tmp_path: Path) -> None:
    repo_root, sha = _create_snapshot_repo(tmp_path)
    release_dir, _ = release_snapshot.build_release(repo_root, sha)
    (repo_root / "live-only.txt").write_text("dirty\n", encoding="utf-8")
    environment = sanitized_git_env() | {
        "LEARN_UK_REPO_ROOT": str(repo_root),
        "GIT_DIR": str(repo_root / ".git"),
        "GIT_WORK_TREE": str(repo_root),
    }

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=release_dir,
        env=environment,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    assert "?? live-only.txt" in result.stdout


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_pruning_preserves_a_live_release_outside_the_newest_three(tmp_path: Path) -> None:
    repo_root, _sha = _create_snapshot_repo(tmp_path)
    releases_dir = release_snapshot.releases_root(repo_root)
    releases_dir.mkdir(parents=True)
    shas = [f"{index:040x}" for index in range(5)]
    release_dirs = []
    for index, sha in enumerate(shas):
        path = releases_dir / sha
        path.mkdir()
        os.utime(path, ns=(index + 1, index + 1))
        release_dirs.append(path)
    (releases_dir / "current").symlink_to(shas[-1], target_is_directory=True)

    listener = subprocess.Popen(
        [
            str(VENV_PYTHON),
            "-c",
            "import socket, time; server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(); time.sleep(30)",
        ],
        cwd=release_dirs[0],
    )
    try:

        def lsof_for_live_listener(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["lsof"],
                returncode=0,
                stdout=f"p{listener.pid}\nn{release_dirs[0]}\n",
                stderr="",
            )

        result = release_snapshot.prune_releases(repo_root, runner=lsof_for_live_listener)
    finally:
        listener.terminate()
        listener.wait(timeout=5)

    assert release_dirs[0].exists(), "a live process owns this old release"
    assert not release_dirs[1].exists()
    assert release_dirs[2].exists() and release_dirs[3].exists() and release_dirs[4].exists()
    assert shas[0] not in result.removed
    assert shas[1] in result.removed


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_safe_join_rejects_a_symlink_escape(tmp_path: Path) -> None:
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (base / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        safe_join(base, "escape", "secret.txt")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_safe_join_allows_declared_live_data_and_preserves_logical_path(tmp_path: Path) -> None:
    repo_root, sha = _create_snapshot_repo(tmp_path)
    release_dir, _ = release_snapshot.build_release(repo_root, sha)

    path = safe_join(release_dir, "curriculum", "l2-uk-en", "a1")

    assert path == release_dir / "curriculum" / "l2-uk-en" / "a1"
    assert path.resolve() == repo_root / "curriculum" / "l2-uk-en" / "a1"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_release_root_attestation_requires_release_layout(tmp_path: Path) -> None:
    repo_root, sha = _create_snapshot_repo(tmp_path)
    release_dir, _ = release_snapshot.build_release(repo_root, sha)
    fake_root = tmp_path / sha
    outside = tmp_path / "outside"
    fake_root.mkdir()
    outside.mkdir()
    (fake_root / MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
    (fake_root / "curriculum").symlink_to(outside, target_is_directory=True)

    assert is_release_root(release_dir)
    assert not is_release_root(fake_root)
    with pytest.raises(ValueError, match="escapes"):
        safe_join(fake_root, "curriculum", "secret.txt")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_safe_join_rejects_nested_undeclared_symlink_from_live_data(tmp_path: Path) -> None:
    repo_root, sha = _create_snapshot_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / "curriculum" / "escape").symlink_to(outside, target_is_directory=True)
    release_dir, _ = release_snapshot.build_release(repo_root, sha)

    with pytest.raises(ValueError, match="escapes"):
        safe_join(release_dir, "curriculum", "escape", "secret.txt")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_safe_join_rejects_planted_manifest_live_data_escape(tmp_path: Path) -> None:
    repo_root, sha = _create_snapshot_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    planted = repo_root / "curriculum" / "planted"
    planted.mkdir()
    (planted / MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
    (planted / "curriculum").symlink_to(outside, target_is_directory=True)
    release_dir, _ = release_snapshot.build_release(repo_root, sha)

    with pytest.raises(ValueError, match="escapes"):
        safe_join(release_dir, "curriculum", "planted", "curriculum", "secret.txt")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_safe_join_rejects_declared_live_data_parent_traversal(tmp_path: Path) -> None:
    repo_root, sha = _create_snapshot_repo(tmp_path)
    release_dir, _ = release_snapshot.build_release(repo_root, sha)

    with pytest.raises(ValueError, match="Invalid path component"):
        safe_join(release_dir, "curriculum", "..", "secret.txt")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_pruning_rechecks_current_release_before_each_deletion(tmp_path: Path) -> None:
    repo_root, _sha = _create_snapshot_repo(tmp_path)
    releases_dir = release_snapshot.releases_root(repo_root)
    releases_dir.mkdir(parents=True)
    shas = [f"{index:040x}" for index in range(5)]
    release_dirs = []
    for index, sha in enumerate(shas):
        path = releases_dir / sha
        path.mkdir()
        os.utime(path, ns=(index + 1, index + 1))
        release_dirs.append(path)
    current = releases_dir / "current"
    current.symlink_to(shas[-1], target_is_directory=True)

    calls = 0

    def lsof_that_switches_current(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        # The fourth sorted release is the first old candidate. Changing
        # current here verifies that prune reads it again before deleting.
        if calls == 4:
            current.unlink()
            current.symlink_to(shas[1], target_is_directory=True)
        return subprocess.CompletedProcess(args=["lsof"], returncode=0, stdout="", stderr="")

    result = release_snapshot.prune_releases(repo_root, runner=lsof_that_switches_current)

    assert calls == 5
    assert release_dirs[1].exists(), "the freshly current release must not be deleted"
    assert not release_dirs[0].exists()
    assert shas[0] in result.removed
    assert shas[1] not in result.removed


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_trusted_join_validates_components_lexically(tmp_path: Path) -> None:
    """trusted_join keeps the lexical guards: no .., no absolute, no separators."""
    from lu_machinery.path_safety import trusted_join

    base = tmp_path / "root"
    base.mkdir()
    assert trusted_join(base, "a", "b.json") == Path(os.path.abspath(base / "a" / "b.json"))
    for bad in ("..", "a/../..", "/abs", "a\x00b", ""):
        with pytest.raises(ValueError):
            trusted_join(base, bad)


def _fake_git(
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    popen_cls: type[subprocess.Popen[bytes]] = subprocess.Popen,
) -> list[subprocess.Popen[bytes]]:
    """Run ``script`` in a real child process in place of ``git archive``."""
    spawned: list[subprocess.Popen[bytes]] = []

    def popen(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        process = popen_cls([sys.executable, "-c", script], **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(release_snapshot.subprocess, "Popen", popen)
    return spawned


def _archive_file(tmp_path: Path, *, fifo: bool = False) -> Path:
    """Write a small tar (``scripts/a.py``, optionally followed by a FIFO)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        payload = b"VALUE = 1\n"
        member = tarfile.TarInfo("scripts/a.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
        if fifo:
            pipe = tarfile.TarInfo("scripts/pipe")
            pipe.type = tarfile.FIFOTYPE
            archive.addfile(pipe)
    path = tmp_path / "archive.tar"
    path.write_bytes(buffer.getvalue())
    return path


def _write_stdout(archive: Path) -> str:
    return f"sys.stdout.buffer.write(open({str(archive)!r}, 'rb').read()); sys.stdout.flush(); "


def _extract_error(tmp_path: Path) -> release_snapshot.ReleaseSnapshotError:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    with pytest.raises(release_snapshot.ReleaseSnapshotError) as exc_info:
        release_snapshot._extract_archive(tmp_path, "0" * 40, staging_dir)
    return exc_info.value


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_deadline_reader_retries_transient_unavailable_read(monkeypatch) -> None:
    """A spurious readiness notification must not become EOF or a reader error."""
    read_fd, write_fd = os.pipe()
    real_read = os.read
    unavailable = True

    def temporarily_unavailable(fd: int, size: int) -> bytes:
        nonlocal unavailable
        if unavailable:
            unavailable = False
            raise BlockingIOError()
        return real_read(fd, size)

    try:
        os.write(write_fd, b"archive bytes")
        reader = release_snapshot._DeadlineReader(read_fd, time.monotonic() + 1.0)
        monkeypatch.setattr(release_snapshot.os, "read", temporarily_unavailable)

        assert reader.read(4096) == b"archive bytes"
        assert reader.expired is False
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_accepts_real_pipe_above_select_fd_limit(monkeypatch, tmp_path: Path) -> None:
    """A valid producer pipe above FD_SETSIZE must still drain and be reaped."""
    if sys.platform != "linux":
        pytest.skip("deterministic pipe capacity and high descriptor control require Linux fcntl")
    import fcntl

    archive = _archive_file(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"\0" * (64 * 1024))
    descriptors: list[int] = []

    class HighDescriptorPipe(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            assert self.stdout is not None
            fd = fcntl.fcntl(self.stdout.fileno(), fcntl.F_DUPFD_CLOEXEC, 1025)
            self.stdout.close()
            self.stdout = os.fdopen(fd, "rb")
            descriptors.append(fd)

    spawned = _fake_git(
        monkeypatch,
        "import sys, signal, fcntl; "
        "signal.signal(signal.SIGPIPE, signal.SIG_DFL); "
        "assert fcntl.fcntl(sys.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096) == 4096; "
        + _write_stdout(archive),
        popen_cls=HighDescriptorPipe,
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    release_snapshot._extract_archive(tmp_path, "0" * 40, staging)

    assert len(descriptors) == 1 and descriptors[0] > 1024
    assert (staging / "scripts/a.py").read_bytes() == b"VALUE = 1\n"
    assert sorted(path.relative_to(staging).as_posix() for path in staging.rglob("*")) == [
        "scripts",
        "scripts/a.py",
    ]
    assert [process.returncode for process in spawned] == [0]
    assert spawned[0].stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0].pid, os.WNOHANG)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_drains_padding_beyond_small_pipe_capacity(monkeypatch, tmp_path: Path) -> None:
    """Closing at tar EOF must not SIGPIPE a producer still writing padding."""
    if sys.platform != "linux":
        pytest.skip("deterministic pipe capacity control requires Linux F_SETPIPE_SZ")
    import fcntl

    if not hasattr(fcntl, "F_SETPIPE_SZ"):
        pytest.skip("fcntl.F_SETPIPE_SZ is unavailable")
    archive = _archive_file(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"\0" * (64 * 1024))
    spawned = _fake_git(
        monkeypatch,
        "import sys, signal, fcntl; "
        "signal.signal(signal.SIGPIPE, signal.SIG_DFL); "
        "capacity = fcntl.fcntl(sys.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096); "
        "assert capacity == 4096; " + _write_stdout(archive),
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    release_snapshot._extract_archive(tmp_path, "0" * 40, staging)

    assert (staging / "scripts/a.py").read_bytes() == b"VALUE = 1\n"
    assert sorted(path.relative_to(staging).as_posix() for path in staging.rglob("*")) == [
        "scripts",
        "scripts/a.py",
    ]
    assert [process.returncode for process in spawned] == [0]
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0].pid, os.WNOHANG)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "damage",
    [
        "invalid-header",
        "short-header",
        "missing-terminator",
        "one-zero-block",
        "short-payload",
        "buffered-tail",
        "pipe-tail",
    ],
)
def test_extract_archive_rejects_corruption_even_when_producer_exits_zero(
    monkeypatch, tmp_path: Path, damage: str
) -> None:
    """tarfile's permissive end-of-iteration must not accept partial archives."""
    archive = _archive_file(tmp_path)
    valid = archive.read_bytes()
    cases = {
        "invalid-header": valid[:1024] + b"X" * 512 + b"\0" * 8192,
        "short-header": valid[:1024] + b"X" * 99,
        "missing-terminator": valid[:1024],
        "one-zero-block": valid[:1536],
        "short-payload": valid[:516],
        "buffered-tail": valid[:2048] + b"X" + b"\0" * 4095,
        "pipe-tail": valid + b"\0" * 32768 + b"X",
    }
    archive.write_bytes(cases[damage])
    spawned = _fake_git(monkeypatch, "import sys; " + _write_stdout(archive))

    err = _extract_error(tmp_path)

    assert err.__cause__ is not None
    assert err.git_status == 0
    assert [process.returncode for process in spawned] == [0]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_rejects_exit_23_after_valid_members(monkeypatch, tmp_path: Path) -> None:
    """Draining a valid tar must preserve the producer's real failure status."""
    archive = _archive_file(tmp_path)
    spawned = _fake_git(
        monkeypatch,
        "import sys; " + _write_stdout(archive) + "sys.stderr.write('producer failed'); sys.exit(23)",
    )

    err = _extract_error(tmp_path)

    assert (tmp_path / "staging/scripts/a.py").read_bytes() == b"VALUE = 1\n"
    assert err.git_status == 23
    assert err.__cause__ is None
    assert err.stderr_tail == "producer failed"
    assert [process.returncode for process in spawned] == [23]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
@pytest.mark.parametrize("symlink", [False, True], ids=["traversal", "symlink"])
def test_extract_archive_drain_preserves_unsafe_member_rejection(monkeypatch, tmp_path: Path, symlink: bool) -> None:
    """A completed producer must not hide an escaping path or symlink."""
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as writer:
        member = tarfile.TarInfo("scripts/link" if symlink else "../escaped")
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "../../escaped"
        writer.addfile(member)
    spawned = _fake_git(monkeypatch, "import sys; " + _write_stdout(archive))

    err = _extract_error(tmp_path)

    assert isinstance(err.__cause__, release_snapshot.ReleaseSnapshotError)
    assert "unsafe" in str(err.__cause__)
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "staging/scripts/link").is_symlink()
    assert [process.returncode for process in spawned] == [0]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
def test_extract_archive_keeps_pax_long_paths(tmp_path: Path) -> None:
    """Strict termination must retain real Git's extended-header support."""
    repo, _ = _create_snapshot_repo(tmp_path)
    relative = Path("scripts") / ("long" * 40 + ".py")
    (repo / relative).write_bytes(b"LONG = True\n")
    _run_git(repo, "add", str(relative))
    _run_git(repo, "commit", "-m", "long archive path")
    sha = _run_git(repo, "rev-parse", "HEAD")
    staging = tmp_path / "staging"
    staging.mkdir()

    release_snapshot._extract_archive(repo, sha, staging)

    assert (staging / relative).read_bytes() == b"LONG = True\n"
    assert (staging / "schemas/example.json").read_bytes() == b"{}\n"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_reports_git_status_and_stderr_when_git_fails(monkeypatch, tmp_path: Path) -> None:
    spawned = _fake_git(monkeypatch, "import sys; sys.stderr.write('fatal: bad revision\\n'); sys.exit(128)")

    err = _extract_error(tmp_path)

    assert err.git_status == 128
    assert err.stderr_tail == "fatal: bad revision"
    assert "git exit status 128" in str(err)
    assert "fatal: bad revision" in str(err)
    assert [process.poll() for process in spawned] == [128]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_reports_reader_error_and_git_status_when_reader_fails(monkeypatch, tmp_path: Path) -> None:
    archive = _archive_file(tmp_path, fifo=True)
    spawned = _fake_git(monkeypatch, "import sys; " + _write_stdout(archive) + "sys.exit(0)")

    err = _extract_error(tmp_path)

    assert (tmp_path / "staging" / "scripts" / "a.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert isinstance(err.__cause__, release_snapshot.ReleaseSnapshotError)
    assert "unsupported entry in git archive: 'scripts/pipe'" in str(err.__cause__)
    assert err.git_status == 0
    assert "reader error: unsupported entry" in str(err)
    assert "git exit status 0" in str(err)
    assert [process.poll() for process in spawned] == [0]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_does_not_hang_when_git_writes_lots_of_stderr_first(monkeypatch, tmp_path: Path) -> None:
    archive = _archive_file(tmp_path)
    spawned = _fake_git(
        monkeypatch,
        "import sys; sys.stderr.write('w' * (4 << 20)); sys.stderr.flush(); " + _write_stdout(archive),
    )
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    release_snapshot._extract_archive(tmp_path, "0" * 40, staging_dir)

    assert (staging_dir / "scripts" / "a.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert [process.poll() for process in spawned] == [0]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_kills_git_that_does_not_exit(monkeypatch, tmp_path: Path) -> None:
    archive = _archive_file(tmp_path)
    monkeypatch.setattr(release_snapshot, "GIT_ARCHIVE_DEADLINE_S", 0.5)
    spawned = _fake_git(monkeypatch, "import sys, time; " + _write_stdout(archive) + "time.sleep(60)")

    err = _extract_error(tmp_path)

    assert err.git_status == -signal.SIGKILL
    assert err.__cause__ is None
    assert "deadline of 0.5s hit; kill sent to git" in str(err)
    assert f"git exit status {-signal.SIGKILL}" in str(err)
    assert [process.poll() for process in spawned] == [-signal.SIGKILL]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_kills_git_that_stalls_mid_stream_at_the_deadline(monkeypatch, tmp_path: Path) -> None:
    archive = _archive_file(tmp_path)
    partial = tmp_path / "partial.tar"
    partial.write_bytes(archive.read_bytes()[:520])  # header plus part of the file body
    monkeypatch.setattr(release_snapshot, "GIT_ARCHIVE_DEADLINE_S", 1.0)
    armed: list[float] = []
    real_timer = release_snapshot.threading.Timer

    def recording_timer(interval: float, function, *args, **kwargs):
        armed.append(interval)
        return real_timer(interval, function, *args, **kwargs)

    monkeypatch.setattr(release_snapshot.threading, "Timer", recording_timer)
    spawned = _fake_git(monkeypatch, "import sys, time; " + _write_stdout(partial) + "time.sleep(60)")

    err = _extract_error(tmp_path)

    assert armed == [1.0]
    assert err.__cause__ is not None  # the tar reader saw EOF mid-member
    assert err.git_status == -signal.SIGKILL
    assert "deadline of 1s hit; kill sent to git" in str(err)
    assert [process.poll() for process in spawned] == [-signal.SIGKILL]


class _ExitsAfterTimedWait(subprocess.Popen):
    """The timed wait expires, and git exits on its own right after it."""

    kills = 0
    timed_out = False

    def wait(self, timeout: float | None = None) -> int:
        if not self.timed_out:
            self.timed_out = True
            os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT)  # exited, not yet reaped
            raise subprocess.TimeoutExpired(self.args, timeout or 0.0)
        return super().wait(timeout)

    def kill(self) -> None:
        type(self).kills += 1
        super().kill()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_reports_real_status_when_git_exits_after_the_timed_wait(monkeypatch, tmp_path: Path) -> None:
    spawned = _fake_git(
        monkeypatch,
        "import sys; sys.stderr.write('fatal: late\\n'); sys.exit(3)",
        popen_cls=_ExitsAfterTimedWait,
    )

    err = _extract_error(tmp_path)

    assert _ExitsAfterTimedWait.kills == 0
    assert err.git_status == 3
    assert "git exit status 3" in str(err)
    assert "kill" not in str(err)
    assert [process.poll() for process in spawned] == [3]


class _IgnoresKill(subprocess.Popen):
    """kill() is sent but git never goes away."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.wait_timeouts: list[float | None] = []

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        return super().wait(timeout=timeout)


class _ExitsZeroOnKill(subprocess.Popen):
    """Deliver a handled signal so a real child exits zero after the deadline."""

    def kill(self) -> None:
        self.send_signal(signal.SIGTERM)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
def test_extract_archive_rejects_zero_status_after_deadline_kill(monkeypatch, tmp_path: Path) -> None:
    """A kill or expired deadline cannot be turned into success by exit zero."""
    archive = _archive_file(tmp_path)
    monkeypatch.setattr(release_snapshot, "GIT_ARCHIVE_DEADLINE_S", 0.5)
    spawned = _fake_git(
        monkeypatch,
        "import sys, time, signal; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        + _write_stdout(archive)
        + "time.sleep(60)",
        popen_cls=_ExitsZeroOnKill,
    )

    err = _extract_error(tmp_path)

    assert err.git_status == 0
    assert err.__cause__ is None
    assert "deadline of 0.5s hit; kill sent to git" in str(err)
    assert [process.returncode for process in spawned] == [0]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.reads_content
@pytest.mark.timeout(30)
@pytest.mark.parametrize("partial", [False, True], ids=["drain", "member-read"])
def test_extract_archive_says_so_when_git_does_not_exit_after_kill(monkeypatch, tmp_path: Path, partial: bool) -> None:
    archive = _archive_file(tmp_path)
    if partial:
        archive.write_bytes(archive.read_bytes()[:520])
    monkeypatch.setattr(release_snapshot, "GIT_ARCHIVE_DEADLINE_S", 0.5)
    monkeypatch.setattr(release_snapshot, "GIT_KILL_WAIT_S", 0.5)
    spawned = _fake_git(
        monkeypatch,
        "import sys, time; " + _write_stdout(archive) + "time.sleep(60)",
        popen_cls=_IgnoresKill,
    )

    try:
        err = _extract_error(tmp_path)

        assert release_snapshot.GIT_KILL_WAIT_S in spawned[0].wait_timeouts
        assert err.git_status is None
        assert (err.__cause__ is not None) == partial
        assert "deadline of 0.5s hit; kill sent to git" in str(err)
        assert "git did not exit after kill" in str(err)
        assert [process.poll() for process in spawned] == [None]
    finally:
        for process in spawned:
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
