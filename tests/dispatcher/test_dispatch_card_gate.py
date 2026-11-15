"""Dispatch refuses open task-card dependencies and lists owned-path overlap with live workers (#583)."""

import json

from lu_machinery.dispatcher import git, store
from tests.dispatcher.test_core_admission import admission_dispatch  # noqa: F401
from tests.helpers.product_tree import configured_roots  # noqa: F401

CARD = "## Outcome\nShip it.\n\n## Dependencies\n- #575 (dispatcher write boundary)\n- #12\n\n## Terminal goal\nmerge\n"


def _github(states, monkeypatch):
    """Answer the dispatcher's GitHub issue reads from ``states``; a missing issue raises like a failed read."""
    def gh(*args, **_kwargs):
        number = int(args[1].rpartition("/")[2])
        if number not in states:
            raise git.GitHubRefused("GitHub admission refused the read")
        return json.dumps({"state": states[number]})

    monkeypatch.setattr(git, "_gh", gh)


def _card(tmp_path):
    path = tmp_path / "card.md"
    path.write_text(CARD, encoding="utf-8")
    return str(path)


def test_open_dependency_is_refused_before_launch(admission_dispatch, tmp_path, monkeypatch, capsys):  # noqa: F811
    """Defect class: a dispatch whose card names an open dependency is admitted."""
    _github({575: "open", 12: "closed"}, monkeypatch)
    assert admission_dispatch.dispatch("--task-card", _card(tmp_path), "--dry-run") == 2
    error = capsys.readouterr().err
    assert "task_card_dependency_refused" in error and "#575 (open)" in error and "#12" not in error
    assert admission_dispatch.spawned == []


def test_override_admits_the_named_dependency_and_is_recorded(admission_dispatch, tmp_path, monkeypatch):  # noqa: F811
    """Defect class: the per-reference override admits the dispatch but the task record does not name it."""
    _github({575: "open", 12: "closed"}, monkeypatch)
    assert admission_dispatch.dispatch("--task-card", _card(tmp_path), "--allow-open-dependency", "#575") == 0
    gate = store.read("adm-worker")["task_card"]
    assert gate["allowed_open_dependencies"] == [gate["dependencies"][0]["ref"]]
    assert gate["dependencies"][0]["ref"].endswith("#575") and gate["dependencies"][1]["state"] == "closed"


def test_github_read_failure_refuses_unless_that_reference_is_allowed(admission_dispatch, tmp_path, monkeypatch, capsys):  # noqa: F811
    """Defect class: a dependency whose state cannot be read is admitted (the gate fails open)."""
    _github({575: "closed"}, monkeypatch)
    assert admission_dispatch.dispatch("--task-card", _card(tmp_path), "--dry-run") == 2
    assert "#12 (unreadable (GitHubRefused))" in capsys.readouterr().err
    assert admission_dispatch.dispatch("--task-card", _card(tmp_path), "--allow-open-dependency", "#12", "--dry-run") == 0


def test_overlap_with_a_live_writer_by_directory_prefix_is_listed_and_refusable(admission_dispatch, monkeypatch, capsys):  # noqa: F811
    """Defect class: a live write task owning a directory is missed when the new task owns a file inside it."""
    live = [
        {"task_id": "live-writer", "status": "running", "mode": "workspace-write", "owned_paths": ["docs/runbooks/"]},
        {"task_id": "live-reader", "status": "running", "mode": "read-only", "owned_paths": ["docs/runbooks"]},
        {"task_id": "done-writer", "status": "done", "mode": "workspace-write", "owned_paths": ["docs/runbooks"]},
        {"task_id": "other-writer", "status": "running", "mode": "workspace-write", "owned_paths": ["docs/runbooks-old"]},
    ]
    monkeypatch.setattr(store, "list_tasks", lambda *_a, **_k: live)
    owned = ("--owned-path", "docs/runbooks/dispatch.md")
    assert admission_dispatch.dispatch(*owned, "--refuse-on-overlap", "--dry-run") == 2
    assert "owned_path_overlap_refused: owned paths overlap live write tasks live-writer" in capsys.readouterr().err
    assert admission_dispatch.dispatch(*owned, "--dry-run") == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["owned_path_overlap"] == ["live-writer"]
    assert "overlap live write tasks: live-writer" in out.err
