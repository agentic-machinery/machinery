"""Retained no-follow read and descriptor-cleanup cases from the retired transport suite."""
from __future__ import annotations
import os
import pytest
from lu_machinery.orchestration import thread_handoff as th


def _read_tree(tmp_path):
    root = tmp_path / "state"
    lineage = root / ".agent/thread-rollovers/cursor/lineage"
    lineage.mkdir(parents=True)
    return root, lineage


@pytest.mark.parametrize("component", [".agent", "thread-rollovers", "agent", "lineage"])
def test_symlink_redirect_refused_without_outside_write(tmp_path, component):
    root, lineage = _read_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"untouched outside")
    targets = {".agent": root / ".agent", "thread-rollovers": root / ".agent/thread-rollovers",
               "agent": lineage.parent, "lineage": lineage}
    target = targets[component]
    target.rename(outside / "original")
    target.symlink_to(outside / "original", target_is_directory=True)
    with pytest.raises(th.BundleReconcileRefused, match="lineage_ancestor_symlink"):
        with th._BundleReconcileTree(root, "cursor", "lineage"):
            pass
    assert (outside / "sentinel").read_bytes() == b"untouched outside"


@pytest.mark.parametrize("kind,code", [
    ("symlink", "reconcile_member_symlink"),
    ("hardlink", "reconcile_member_hardlink"),
    ("fifo", "reconcile_fifo"),
])
def test_member_leaf_is_never_overwritten(tmp_path, kind, code):
    root, lineage = _read_tree(tmp_path)
    outside = tmp_path / "sentinel"
    outside.write_bytes(b"untouched outside")
    leaf = lineage / "handoff.md"
    if kind == "symlink":
        leaf.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, leaf)
    else:
        os.mkfifo(leaf)
    with pytest.raises(th.BundleReconcileRefused, match=code):
        with th._BundleReconcileTree(root, "cursor", "lineage") as tree:
            tree.read_path(".agent/thread-rollovers/cursor/lineage/handoff.md")
    assert outside.read_bytes() == b"untouched outside"


def test_enter_keeps_directory_error_when_fd_close_fails(tmp_path, monkeypatch, caplog):
    """Defect class: os.close during _BundleReconcileTree entry cleanup replaced the entry error."""
    state = tmp_path / "state"
    state.mkdir()

    def fail_directory(self, *args, **kwargs):
        raise OSError("injected directory failure")

    def fail_close(fd):
        raise OSError("injected fd close failure")

    monkeypatch.setattr(th._BundleReconcileTree, "directory", fail_directory)
    monkeypatch.setattr(os, "close", fail_close)
    with (
        caplog.at_level("WARNING", logger="lu_machinery.orchestration.thread_handoff"),
        pytest.raises(OSError, match="injected directory failure") as raised,
        th._BundleReconcileTree(state, "cursor", "lineage"),
    ):
        pass
    assert any("injected fd close failure" in note for note in raised.value.__notes__)
    assert "bundle reconcile fd cleanup failed" in caplog.text


def test_body_error_survives_exit_fd_close_failure(tmp_path, monkeypatch, caplog):
    """Defect class: os.close in _BundleReconcileTree.__exit__ replaced the with-body error."""
    state = tmp_path / "state"
    state.mkdir()
    real_close = os.close

    def fail_close(fd):
        real_close(fd)
        raise OSError("injected fd close failure")

    monkeypatch.setattr(os, "close", fail_close)
    failure = OSError("injected body failure")
    with (
        caplog.at_level("WARNING", logger="lu_machinery.orchestration.thread_handoff"),
        pytest.raises(OSError, match="injected body failure") as raised,
        th._BundleReconcileTree(state, "cursor", "lineage"),
    ):
        raise failure
    assert raised.value is failure
    assert any("injected fd close failure" in note for note in raised.value.__notes__)
    assert "bundle reconcile fd cleanup failed" in caplog.text
