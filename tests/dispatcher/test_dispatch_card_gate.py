"""Dispatch refuses open task-card dependencies and lists owned-path overlap with live workers (#583)."""

import json

from lu_machinery.dispatcher import git, store
from tests.dispatcher.test_core_admission import admission_dispatch  # noqa: F401
from tests.helpers.product_tree import configured_roots  # noqa: F401

# The sandbox repository's GitHub field is a local remote path, so the card names fully qualified references.
OPEN, CLOSED = "learn-ukrainian/machinery#575", "learn-ukrainian/machinery#12"


def _github(states, monkeypatch):
    """Answer the dispatcher's GitHub issue reads from ``states``; a missing issue raises like a refused read."""
    def gh(*args, **_kwargs):
        assert args[0] == "api" and args[1].startswith("repos/learn-ukrainian/machinery/issues/"), args
        number = int(args[1].rpartition("/")[2])
        if number not in states:
            raise git.GitHubRefused("GitHub admission refused the read")
        return json.dumps({"state": states[number]})

    monkeypatch.setattr(git, "_gh", gh)


def _card(tmp_path, *entries):
    path = tmp_path / "card.md"
    lines = "".join(f"- {entry}\n" for entry in entries)
    path.write_text(f"## Outcome\nShip it.\n\n## Dependencies\n{lines}\n## Terminal goal\nmerge\n", encoding="utf-8")
    return str(path)


def test_open_or_unparseable_dependency_is_refused_before_launch(admission_dispatch, tmp_path, monkeypatch, capsys):  # noqa: F811
    """Defect class: a dispatch whose card names an open dependency (or one the gate cannot read as a reference) is admitted."""
    _github({575: "open", 12: "closed"}, monkeypatch)
    card = _card(tmp_path, OPEN, CLOSED, "Follow-up for #12")
    assert admission_dispatch.dispatch("--task-card", card, "--dry-run") == 2
    error = capsys.readouterr().err
    assert "task_card_dependency_refused" in error
    assert f"{OPEN} (open)" in error and "Follow-up for #12 (unparseable)" in error and f"{CLOSED} (" not in error
    assert admission_dispatch.spawned == []


def test_override_admits_the_named_dependency_and_is_recorded(admission_dispatch, tmp_path, monkeypatch):  # noqa: F811
    """Defect class: the per-reference override admits the dispatch but the task record does not name it."""
    _github({575: "open", 12: "closed"}, monkeypatch)
    assert admission_dispatch.dispatch("--task-card", _card(tmp_path, OPEN, CLOSED), "--allow-open-dependency", OPEN) == 0
    gate = store.read("adm-worker")["task_card"]
    assert gate["dependencies"] == [{"ref": OPEN, "state": "open"}, {"ref": CLOSED, "state": "closed"}]
    assert gate["allowed_open_dependencies"] == [OPEN]


def test_github_read_failure_refuses_unless_that_reference_is_allowed(admission_dispatch, tmp_path, monkeypatch, capsys):  # noqa: F811
    """Defect class: a dependency whose state cannot be read is admitted (the gate fails open)."""
    _github({575: "closed"}, monkeypatch)
    card = _card(tmp_path, OPEN, CLOSED)
    assert admission_dispatch.dispatch("--task-card", card, "--dry-run") == 2
    assert f"{CLOSED} (unreadable (GitHubRefused))" in capsys.readouterr().err
    assert admission_dispatch.dispatch("--task-card", card, "--allow-open-dependency", CLOSED, "--dry-run") == 0


def test_overlap_with_a_live_writer_by_directory_prefix_is_listed_and_refusable(admission_dispatch, monkeypatch, capsys):  # noqa: F811
    """Defect class: a live write task owning a common path is missed (a directory above, or a pattern sharing a descendant)."""
    def writer(task_id, owned, repo="public", status="running", mode="workspace-write"):
        return {"task_id": task_id, "status": status, "mode": mode, "repo": repo, "owned_paths": owned}

    live = [
        writer("dir-writer", ["docs/runbooks/"]),
        writer("descendant-writer", ["docs/*/sub/x.md"]),
        writer("pattern-writer", ["docs/runbooks/*b.py"]),
        writer("live-reader", ["docs/runbooks"], mode="read-only"),
        writer("done-writer", ["docs/runbooks"], status="done"),
        writer("sibling-writer", ["docs/runbooks-old"]),
        writer("disjoint-pattern-writer", ["docs/runbooks/c*.txt"]),
        writer("other-repo-writer", ["docs/runbooks"], repo="private"),
    ]
    monkeypatch.setattr(store, "list_tasks", lambda *_a, **_k: live)
    owned = ("--owned-path", "docs/runbooks/sub", "--owned-path", "docs/runbooks/a*.py")
    overlap = "descendant-writer, dir-writer, pattern-writer"
    assert admission_dispatch.dispatch(*owned, "--refuse-on-overlap", "--dry-run") == 2
    assert f"owned_path_overlap_refused: owned paths overlap live write tasks {overlap}" in capsys.readouterr().err
    assert admission_dispatch.dispatch(*owned, "--dry-run") == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["owned_path_overlap"] == overlap.split(", ")
    assert f"overlap live write tasks: {overlap}" in out.err
