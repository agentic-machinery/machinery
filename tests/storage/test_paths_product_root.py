"""Artifact paths come from the configured product root, never from the install location.

Defect class: the default artifact store derived from ``paths.__file__`` points into
site-packages when installed; it must follow ``LU_PRODUCT_ROOT`` and refuse when unset.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
import pytest
from lu_machinery.common.repo_root import PRODUCT_ROOT_ENV, ProductRootError
from lu_machinery.storage import artifacts, paths
import json
import os
import sqlite3
from lu_machinery.storage.topology import REQUIRED_BULK_MARKERS
from lu_machinery.storage.topology import ActiveDatabaseNetworkError
from lu_machinery.storage.topology import require_local_active_sources_db
from lu_machinery.storage.topology import resolve_active_sources_db
from lu_machinery.storage.topology import resolve_bulk_root
from lu_machinery.storage.topology import resolve_topology
from tests.helpers.python import project_python as project_python
from tests.helpers.product_tree import configured_roots as configured_roots

def _git_product(tmp_path: Path) -> Path:
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init", "-q", str(product)], check=True)
    return product


def test_store_root_follows_configured_product_root(tmp_path, monkeypatch):
    product = _git_product(tmp_path)
    monkeypatch.setenv(PRODUCT_ROOT_ENV, str(product))
    monkeypatch.delenv("LU_ARTIFACT_STORE", raising=False)
    before = sorted(tmp_path.rglob("*"))
    store = paths.artifact_store_root()
    assert store == product.resolve() / "data/.artifact-store"
    assert not Path(paths.__file__).resolve().parent.parent.parent in store.parents
    assert sorted(tmp_path.rglob("*")) == before


def test_unset_product_root_refuses(monkeypatch):
    monkeypatch.delenv(PRODUCT_ROOT_ENV, raising=False)
    with pytest.raises(ProductRootError):
        paths.artifact_store_root()
    with pytest.raises(ProductRootError):
        artifacts.write_artifact(Path("x"), "g", "p", lambda p: None)


def test_no_install_location_constants():
    assert not hasattr(paths, "ROOT")
    assert not hasattr(paths, "DATA_ROOT")
    assert not hasattr(artifacts, "ROOT")


REPO_ROOT = Path(__file__).resolve().parents[2]


def _project_python() -> str:
    """The shared project interpreter (the one running pytest)."""
    return project_python()


def _make_bulk_root(path: Path, *, markers: tuple[str, ...] = REQUIRED_BULK_MARKERS) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for marker in markers:
        (path / marker).mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.usefixtures('configured_roots')
def test_smb_preferred_when_marker_valid(tmp_path: Path) -> None:
    smb = _make_bulk_root(tmp_path / "smb")
    drive = _make_bulk_root(tmp_path / "drive")
    result = resolve_bulk_root(
        env={},
        smb_candidates=[smb],
        drive_candidates=[drive],
    )
    assert result.available is True
    assert result.source == "smb"
    assert result.path == smb


@pytest.mark.usefixtures('configured_roots')
def test_drive_fallback_when_smb_absent(tmp_path: Path) -> None:
    missing_smb = tmp_path / "smb-missing"
    drive = _make_bulk_root(tmp_path / "drive")
    result = resolve_bulk_root(
        env={},
        smb_candidates=[missing_smb],
        drive_candidates=[drive],
    )
    assert result.available is True
    assert result.source == "gdrive"
    assert result.path == drive


@pytest.mark.usefixtures('configured_roots')
def test_unavailable_when_neither_present(tmp_path: Path) -> None:
    result = resolve_bulk_root(
        env={},
        smb_candidates=[tmp_path / "no-smb"],
        drive_candidates=[tmp_path / "no-drive"],
    )
    assert result.available is False
    assert result.source == "unavailable"
    assert result.path is None
    assert result.reason == "no_marker_valid_bulk_root"


@pytest.mark.usefixtures('configured_roots')
def test_smb_present_but_missing_markers_falls_through_to_drive(tmp_path: Path) -> None:
    smb = tmp_path / "smb"
    smb.mkdir()
    (smb / "literary_texts").mkdir()
    # textbook_chunks intentionally absent
    drive = _make_bulk_root(tmp_path / "drive")
    result = resolve_bulk_root(
        env={},
        smb_candidates=[smb],
        drive_candidates=[drive],
    )
    assert result.available is True
    assert result.source == "gdrive"
    assert result.path == drive
    smb_reports = [c for c in result.candidates if c.kind == "smb"]
    assert smb_reports
    assert smb_reports[0].marker_valid is False
    assert "textbook_chunks" in smb_reports[0].missing_markers


@pytest.mark.usefixtures('configured_roots')
def test_ambiguous_drive_roots_unavailable(tmp_path: Path) -> None:
    d1 = _make_bulk_root(tmp_path / "drive1")
    d2 = _make_bulk_root(tmp_path / "drive2")
    result = resolve_bulk_root(
        env={},
        smb_candidates=[],
        drive_candidates=[d1, d2],
    )
    assert result.available is False
    assert result.reason == "ambiguous_gdrive_roots"


@pytest.mark.usefixtures('configured_roots')
def test_lu_bulk_root_override(tmp_path: Path) -> None:
    override = _make_bulk_root(tmp_path / "explicit")
    other = _make_bulk_root(tmp_path / "smb")
    result = resolve_bulk_root(
        env={"LU_BULK_ROOT": str(override)},
        smb_candidates=[other],
        drive_candidates=[],
    )
    assert result.available is True
    assert result.source == "override"
    assert result.path == override


@pytest.mark.usefixtures('configured_roots')
def test_lu_gdrive_data_override_wins_over_caller_drive_candidates(
    tmp_path: Path,
) -> None:
    """LU_GDRIVE_DATA must beat caller drive_candidates (env first)."""
    env_root = _make_bulk_root(tmp_path / "env-drive")
    caller_root = _make_bulk_root(tmp_path / "caller-drive")
    result = resolve_bulk_root(
        env={"LU_GDRIVE_DATA": str(env_root)},
        smb_candidates=[],
        drive_candidates=[caller_root],
    )
    assert result.available is True
    assert result.source == "gdrive"
    assert result.path == env_root
    assert result.reason == "LU_GDRIVE_DATA marker-valid"
    # Caller candidate must not be selected when env override is set.
    assert result.path != caller_root


@pytest.mark.usefixtures('configured_roots')
def test_invalid_lu_gdrive_data_fails_closed_no_drive_fallback(
    tmp_path: Path,
) -> None:
    """Invalid LU_GDRIVE_DATA must not fall through to other Drive roots."""
    invalid = tmp_path / "invalid-drive"
    invalid.mkdir()
    (invalid / "literary_texts").mkdir()
    # textbook_chunks intentionally absent → not marker-valid
    fallback = _make_bulk_root(tmp_path / "auto-drive")
    result = resolve_bulk_root(
        env={"LU_GDRIVE_DATA": str(invalid)},
        smb_candidates=[],
        drive_candidates=[fallback],
    )
    assert result.available is False
    assert result.path is None
    assert result.source == "unavailable"
    assert result.reason == "drive_override_not_marker_valid"
    gdrive_reports = [c for c in result.candidates if c.kind == "gdrive"]
    assert len(gdrive_reports) == 1
    assert gdrive_reports[0].path == str(invalid)
    assert gdrive_reports[0].marker_valid is False


@pytest.mark.usefixtures('configured_roots')
def test_refuse_network_sources_db_override(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "data" / "sources.db").write_bytes(b"sqlite")
    network = Path("/Volumes/UkrainianData/raw-sources/learn-ukrainian-data/sources.db")
    result = resolve_active_sources_db(
        repo,
        env={"LU_SOURCES_DB": str(network)},
        refuse_network=True,
    )
    assert result.refused_network is True
    assert result.is_local is True
    assert result.path == repo / "data" / "sources.db"
    with pytest.raises(ActiveDatabaseNetworkError):
        require_local_active_sources_db(
            repo,
            env={"LU_SOURCES_DB": str(network)},
        )


@pytest.mark.usefixtures('configured_roots')
def test_topology_status_json_roundtrip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "scripts").mkdir()
    status = resolve_topology(
        repository_root=repo,
        env={},
        smb_candidates=[tmp_path / "missing-smb"],
        drive_candidates=[tmp_path / "missing-drive"],
    )
    payload = status.to_dict()
    assert payload["schema"] == "storage-topology.status.v1"
    assert payload["bulk_root"]["available"] is False
    assert payload["active_database"]["is_local"] is True
    # Ensure JSON serializable.
    json.dumps(payload)


@pytest.mark.usefixtures('configured_roots')
def test_cli_status_json_subprocess(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "scripts").mkdir()
    env = os.environ.copy()
    env["LU_BULK_ROOT"] = str(tmp_path / "missing-bulk")
    env.pop("LU_SMB_BULK_ROOT", None)
    env.pop("LU_GDRIVE_DATA", None)
    proc = subprocess.run(
        [
            str(_project_python()),
            "-m",
            "lu_machinery.storage",
            "status",
            "--json",
            "--repo",
            str(repo),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema"] == "storage-topology.status.v1"
    assert "active_database" in payload
    assert "bulk_root" in payload


@pytest.mark.usefixtures('configured_roots')
def test_status_reports_sources_db_size_and_journal_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "scripts").mkdir()
    db = repo / "data" / "sources.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    payload = resolve_topology(
        repository_root=repo,
        env={},
        smb_candidates=[tmp_path / "missing-smb"],
        drive_candidates=[tmp_path / "missing-drive"],
    ).to_dict()
    active = payload["active_database"]
    assert active["journal_mode"] == "wal"
    assert active["size_bytes"] == db.stat().st_size > 0
    assert isinstance(active["wal_bytes"], int)
    json.dumps(payload)

    (tmp_path / "elsewhere").mkdir(exist_ok=True)
    missing = resolve_topology(
        repository_root=tmp_path / "elsewhere",
        env={},
        smb_candidates=[],
        drive_candidates=[],
    ).to_dict()["active_database"]
    assert (missing["size_bytes"], missing["journal_mode"], missing["wal_bytes"]) == (None, None, None)
