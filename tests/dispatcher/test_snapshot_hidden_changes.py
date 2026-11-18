"""Exit snapshot and removal check of tracked changes that git status hides (Refs #100).

Defect class: ``snapshot_delivery`` read only ``git status --porcelain``, which trusts the assume-unchanged and
skip-worktree index flags and the submodule ignore settings, so a worker that exited with such uncommitted
tracked work was reported clean (``worktree_dirty_on_exit=False``) and passed the delivery rescue.
"""

import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lu_machinery.dispatcher import git

# Converted on checkout (UTF-8 stored, UTF-16LE in the tree); the name has a leading blank and a carriage return.
ENCODED = " encoded\r.txt"


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", str(cwd), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(path):
    _git(path.parent, "init", "-q", "-b", "main", str(path))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        _git(path, "config", key, value)
    return path


def _dirty(work):
    record = {"worktree_base_sha": _git(work, "rev-parse", "HEAD")}
    dirty = git.snapshot_delivery(SimpleNamespace(worktree=work, mode="read-only"), record)["worktree_dirty_on_exit"]
    changed, untracked = git._tree_changes(work)
    assert bool(changed) is dirty
    assert untracked == []
    return dirty


@pytest.fixture
def work(tmp_path):
    work = _repo(tmp_path / "work")
    (work / "tracked.txt").write_text("committed\n")
    (work / ".gitattributes").write_text("*encoded* working-tree-encoding=UTF-16LE\n")
    (work / ENCODED).write_bytes("кодоване\n".encode("utf-16-le"))
    (work / "excluded").mkdir()
    (work / "excluded" / "data.txt").write_text("outside the sparse rules\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    return work


def _submodule(tmp_path, work, ignore):
    sub = _repo(tmp_path / "sub")
    (sub / "inner.txt").write_text("inner\n")
    _git(sub, "add", ".")
    _git(sub, "commit", "-q", "-m", "inner")
    _git(work, "submodule", "add", "-q", str(sub), "sub")
    _git(work, "commit", "-q", "-m", "submodule")
    _git(work, "config", "submodule.sub.ignore", ignore)
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        _git(work / "sub", "config", key, value)
    assert _dirty(work) is False
    return work / "sub"


def test_clean_tree_reads_clean(work):
    """A clean tree stays clean, also as a dispatch tree: sparse-excluded entries and unedited flagged ones,
    including one converted on checkout whose name a cat-file batch request or a text read would alter."""
    assert _dirty(work) is False
    (work / "tracked.txt").write_text("plain edit\n")
    assert _dirty(work) is True
    (work / "tracked.txt").write_text("committed\n")
    _git(work, "sparse-checkout", "set", "--no-cone", "--", "/*", "!/excluded/")
    assert not (work / "excluded").exists()
    _git(work, "update-index", "--assume-unchanged", "tracked.txt", ENCODED)
    assert _dirty(work) is False


def test_skip_worktree_deletion_reads_dirty(work):
    _git(work, "update-index", "--skip-worktree", "tracked.txt")
    (work / "tracked.txt").unlink()
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    # An absent skip-worktree entry still counts when the sparse rules include it.
    _git(work, "sparse-checkout", "set", "--no-cone", "--", "/*", "!/excluded/")
    _git(work, "update-index", "--skip-worktree", "tracked.txt")
    (work / "tracked.txt").unlink(missing_ok=True)
    assert _dirty(work) is True


def test_skip_worktree_edit_reads_dirty(work):
    _git(work, "update-index", "--skip-worktree", "tracked.txt")
    (work / "tracked.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    (work / "tracked.txt").write_text("committed\n")
    assert _dirty(work) is False


def test_assume_unchanged_edit_reads_dirty(work):
    """Also an edit that leaves the stored blob's own bytes in a file converted on checkout."""
    _git(work, "update-index", "--assume-unchanged", "tracked.txt", ENCODED)
    (work / "tracked.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    (work / "tracked.txt").write_text("committed\n")
    assert _dirty(work) is False
    (work / ENCODED).write_bytes(subprocess.run(["git", "-C", str(work), "show", f"HEAD:{ENCODED}"],
                                                check=True, capture_output=True).stdout)
    assert _dirty(work) is True


def test_flagged_lf_rewrite_under_autocrlf_reads_clean(work):
    """Defect class: a flagged file rewritten with LF under core.autocrlf=true was reported dirty
    although that difference is the committed form (#100 R82). Stripping CR then hid a real edit
    Git does not convert: a stored CR blob, a lone CR, a non-printable file, -crlf, and a
    working-tree encoding."""
    _git(work, "config", "core.autocrlf", "true")
    _git(work, "update-index", "--assume-unchanged", "tracked.txt")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is False
    (work / "tracked.txt").write_text("real edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    (work / "tracked.txt").write_bytes(b"committed\r\n")
    assert _dirty(work) is False
    _git(work, "config", "core.autocrlf", "false")
    assert _dirty(work) is True
    _git(work, "update-index", "--no-assume-unchanged", "tracked.txt")
    (work / "tracked.txt").write_bytes(b"committed\r\n")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "store crlf")
    _git(work, "config", "core.autocrlf", "true")
    _git(work, "update-index", "--assume-unchanged", "tracked.txt")
    assert _dirty(work) is True
    (work / "tracked.txt").write_bytes(b"committed\n")
    assert _dirty(work) is True
    (work / "tracked.txt").write_bytes(b"a\r\nb\n")
    _git(work, "update-index", "--no-assume-unchanged", "tracked.txt")
    _git(work, "config", "core.autocrlf", "false")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "mixed")
    _git(work, "config", "core.autocrlf", "true")
    _git(work, "update-index", "--assume-unchanged", "tracked.txt")
    (work / "tracked.txt").write_bytes(b"a\nb\n")
    assert _dirty(work) is True
    _git(work, "update-index", "--no-assume-unchanged", "tracked.txt")
    _git(work, "config", "core.autocrlf", "false")
    (work / "tracked.txt").write_bytes(b"committed\n")
    (work / ".gitattributes").write_text((work / ".gitattributes").read_text() + "tracked.txt -text\n")
    _git(work, "add", "tracked.txt", ".gitattributes")
    _git(work, "commit", "-q", "-m", "disable conversion")
    _git(work, "config", "core.autocrlf", "true")
    _git(work, "update-index", "--assume-unchanged", "tracked.txt", ".gitattributes")
    (work / "tracked.txt").write_bytes(b"committed\r\n")
    assert _dirty(work) is True
    _git(work, "update-index", "--no-assume-unchanged", "tracked.txt", ".gitattributes")
    _git(work, "config", "core.autocrlf", "false")
    (work / ".gitattributes").write_text("*encoded* working-tree-encoding=UTF-16LE\n")
    (work / "tracked.txt").write_bytes(b"a\rb\n")
    _git(work, "add", "tracked.txt", ".gitattributes")
    _git(work, "commit", "-q", "-m", "lone cr")
    _git(work, "config", "core.autocrlf", "true")
    _git(work, "update-index", "--assume-unchanged", "tracked.txt")
    (work / "tracked.txt").write_bytes(b"a\rb\r\n")
    assert _dirty(work) is True
    _git(work, "update-index", "--no-assume-unchanged", "tracked.txt")
    _git(work, "config", "core.autocrlf", "false")
    (work / "tracked.txt").write_bytes(b"\x01a\n")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "nonprintable")
    _git(work, "config", "core.autocrlf", "true")
    _git(work, "update-index", "--assume-unchanged", "tracked.txt")
    (work / "tracked.txt").write_bytes(b"\x01a\r\n")
    assert _dirty(work) is True
    _git(work, "update-index", "--no-assume-unchanged", "tracked.txt")
    _git(work, "config", "core.autocrlf", "false")
    (work / "tracked.txt").write_bytes(b"committed\n")
    (work / ".gitattributes").write_text(
        (work / ".gitattributes").read_text() + "tracked.txt -crlf\n"
    )
    _git(work, "add", "tracked.txt", ".gitattributes")
    _git(work, "commit", "-q", "-m", "legacy crlf off")
    _git(work, "config", "core.autocrlf", "true")
    _git(work, "update-index", "--assume-unchanged", "tracked.txt", ".gitattributes")
    (work / "tracked.txt").write_bytes(b"committed\r\n")
    assert _dirty(work) is True
    _git(work, "update-index", "--no-assume-unchanged", "tracked.txt", ".gitattributes")
    _git(work, "config", "core.autocrlf", "false")
    (work / ".gitattributes").write_text("*encoded* working-tree-encoding=UTF-16LE\ntracked.txt working-tree-encoding=ISO-8859-1\n")
    (work / "tracked.txt").write_bytes("é\n".encode("latin1"))
    _git(work, "add", "tracked.txt", ".gitattributes")
    _git(work, "commit", "-q", "-m", "encoded")
    _git(work, "config", "core.autocrlf", "true")
    _git(work, "update-index", "--assume-unchanged", "tracked.txt", ".gitattributes")
    (work / "tracked.txt").write_bytes("é\n".encode())
    assert _dirty(work) is True


def test_submodule_edit_with_ignore_dirty_reads_dirty(tmp_path, work):
    """Also an edit that the submodule's own assume-unchanged flag hides from its status, with or without the
    submodule's gitfile."""
    sub = _submodule(tmp_path, work, "dirty")
    (sub / "inner.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    (sub / "inner.txt").write_text("inner\n")
    _git(sub, "update-index", "--assume-unchanged", "inner.txt")
    assert _dirty(work) is False
    (sub / "inner.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain", "--ignore-submodules=none") == ""
    assert _dirty(work) is True
    # Without its gitfile the populated submodule is invisible to the superproject's status.
    (sub / ".git").unlink()
    assert _git(work, "status", "--porcelain", "--ignore-submodules=none") == ""
    assert _dirty(work) is True


def test_new_submodule_commit_with_ignore_all_reads_dirty(tmp_path, work):
    sub = _submodule(tmp_path, work, "all")
    (sub / "inner.txt").write_text("new commit\n")
    _git(sub, "commit", "-q", "-am", "inner change")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True



def test_salvage_hold_keeps_a_tree_with_an_unpushed_commit_in_a_tracked_submodule(tmp_path, work):
    """Defect class: _salvage_hold looked only at untracked or ignored nested repositories, so salvage could
    drop a local commit inside a tracked (mode 160000) submodule (#100 R88 F2)."""
    sub = _submodule(tmp_path, work, "none")
    assert git._salvage_hold(work, work, []) is None
    _git(work / "sub", "config", "user.name", "t")
    _git(work / "sub", "config", "user.email", "t@example.invalid")
    _git(work / "sub", "commit", "-q", "--allow-empty", "-m", "local only")
    assert git._salvage_hold(work, work, []) == ("nested_repository_unpushed", "sub")
    assert sub.is_dir()


def test_salvage_hold_never_runs_a_submodules_own_configured_command(tmp_path, work):
    """Defect class: the hold's nested status ran the worker-controlled submodule's core.fsmonitor command
    in the supervisor (#100 R88 F2, review F1)."""
    _submodule(tmp_path, work, "none")
    marker = tmp_path / "ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    _git(work / "sub", "config", "core.fsmonitor", str(hook))
    assert git._salvage_hold(work, work, []) is None
    assert not marker.exists()


def test_removal_reports_a_recursion_error_as_unknown_not_error(tmp_path, monkeypatch):
    """Defect class: nested submodules deeper than the recursion limit raised RecursionError out of
    settle_worktree instead of retaining the tree as unknown (#100 R88 F1)."""
    monkeypatch.setattr(git, "_retention_held", lambda _record: False)
    monkeypatch.setattr(git, "resolve_repo", lambda _repo: (tmp_path, None))
    monkeypatch.setattr(git, "_tree_changes", Mock(side_effect=RecursionError))
    record = {"task_id": "deep", "mode": "workspace-write", "worktree_path": str(tmp_path),
              "worktree_branch": "claude/deep", "worktree_base_sha": "0" * 40}
    disposition = git.settle_worktree(record)
    assert (disposition["worktree_disposition"], disposition["worktree_disposition_reason"]) == ("retained", "unknown")


# Distinct names a text read of Git's output folds into one another (CR and CRLF become LF).
LINE_BREAK_NAMES = ["scratch/cr\rname", "scratch/cr\nname", "scratch/crlf\r\nname", "scratch/crlf\nname",
                    # A text read of the snapshot's list fails on the first and splits the second (#812 round 1).
                    os.fsdecode(b"scratch/bad\xffname"), "scratch/line\u2028separator"]


@pytest.mark.parametrize("case", ["untracked", "ignored", "stat-hidden", "attributes-file", "attributes-file-global",
                                  "filtered", "filtered-global", "global-conversion"])
def test_snapshot_and_removal_reads_reveal_at_least_what_main_did(tmp_path, monkeypatch, case):
    """Defect class: a worker's stat settings hid a tracked edit from the exit snapshot and from removal, and
    removal's text read folded CR and CRLF in untracked names, so it preserved names that do not exist (#524).
    Each read stays the one main used (the snapshot's execution-safe read, removal's plain-Git read with every
    config scope and the tree's filters) with controls that only reveal more, so neither reads a tree clean that
    main read changed: an edit only a filter or a global conversion shows still keeps the tree (#807 rounds 1-3)."""
    global_config = tmp_path / "global"
    global_config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))  # this test's own global config, no system one
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    work = _repo(tmp_path / "tree")
    _git(work, "config", "core.quotePath", "false")  # status prints high bytes in names as they are
    (work / ".gitignore").write_text("*.log\n")
    (work / "delivered.txt").write_text("delivery\n")
    if case.startswith("attributes-file"):  # checked out as CRLF only through a configured attributes file
        (tmp_path / "attributes").write_text("*.txt text eol=crlf\n")
        if case == "attributes-file":
            _git(work, "config", "core.attributesFile", str(tmp_path / "attributes"))
        else:
            global_config.write_text(f"[core]\n\tattributesFile = {tmp_path / 'attributes'}\n")
        (work / "delivered.txt").write_bytes(b"delivery\r\n")
    elif case == "global-conversion":  # stored with CRLF before any attribute applied
        (work / "delivered.txt").write_bytes(b"delivery\r\n")
    elif case.startswith("filtered"):  # a reversible filter: the checkout holds A, the stored blob B
        if case == "filtered":
            for kind in ("clean", "smudge"):
                _git(work, "config", f"filter.swap.{kind}", "tr AB BA")
            (work / ".gitattributes").write_text("sample.txt filter=swap\n")
        else:  # the driver and the attribute only in the global config and its attributes file
            (tmp_path / "global-attributes").write_text("sample.txt filter=swap\n")
            global_config.write_text(f'[filter "swap"]\n\tclean = tr AB BA\n\tsmudge = tr AB BA\n'
                                     f"[core]\n\tattributesFile = {tmp_path / 'global-attributes'}\n")
        (work / "sample.txt").write_text("A\n")
    os.utime(work / "delivered.txt", (1_000_000_000, 1_000_000_000))  # older than the index: not racily clean
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "delivery")
    leftovers = {"untracked": LINE_BREAK_NAMES, "ignored": ["scratch/out.log"]}.get(case, [])
    for name in leftovers:
        (work / name).parent.mkdir(exist_ok=True)
        (work / name).write_text(f"left over {name!r}\n")
    if case.startswith("attributes-file"):
        os.utime(work / "delivered.txt")  # new timestamps, same bytes: status re-reads it through the attributes
        assert _git(work, "status", "--porcelain") == ""
    elif case == "global-conversion":
        (tmp_path / "global-attributes").write_text("delivered.txt text\n")
        global_config.write_text(f"[core]\n\tattributesFile = {tmp_path / 'global-attributes'}\n")
        os.utime(work / "delivered.txt")  # same bytes; plain Git now normalizes them, so they differ from the blob
        assert _git(work, "status", "--porcelain") == "M delivered.txt"
    elif case.startswith("filtered"):
        (work / "sample.txt").write_text("B\n")  # through the filter this is a change; raw, it equals the blob
        assert _git(work, "status", "--porcelain") == "M sample.txt"
    elif case == "stat-hidden":
        # Same size and timestamps on a new inode: only the stat checks the worker switches off can see it.
        before, rewrite = (work / "delivered.txt").stat(), work / "rewrite.tmp"
        rewrite.write_text("DELIVERY\n")
        os.utime(rewrite, ns=(before.st_atime_ns, before.st_mtime_ns))
        rewrite.replace(work / "delivered.txt")
        _git(work, "config", "core.trustctime", "false")
        _git(work, "config", "core.checkStat", "minimal")
        # Hidden from a plain status; --no-optional-locks keeps that probe from refreshing the index.
        assert "delivered.txt" not in _git(work, "--no-optional-locks", "status", "--porcelain")
    record = {"worktree_base_sha": _git(work, "rev-parse", "HEAD")}
    observed = git.snapshot_delivery(SimpleNamespace(worktree=work, mode="read-only"), record)
    # The snapshot reads only the repository's config with filters off, as main's did: a global attributes file
    # is unseen there (attributes-file-global), and a filtered or globally converted edit is unseen too.
    assert observed["worktree_dirty_on_exit"] is (case in {"untracked", "stat-hidden", "attributes-file-global"})
    assert observed["worktree_untracked_only_on_exit"] is (case == "untracked")
    changed, untracked = git._tree_changes(work)
    assert changed == {"stat-hidden": [" M delivered.txt"], "global-conversion": [" M delivered.txt"],
                       "filtered": [" M sample.txt"], "filtered-global": [" M sample.txt"]}.get(case, [])
    if case == "stat-hidden":  # salvage stages the edit the reader reports, never HEAD's tree again
        assert git._snapshot_tree(work, "probe") != _git(work, "rev-parse", "HEAD^{tree}")
    assert sorted(untracked) == sorted(LINE_BREAK_NAMES if case == "untracked" else [])
