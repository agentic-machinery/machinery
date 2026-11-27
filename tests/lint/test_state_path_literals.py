"""The lint rejects a new literal store path and accepts the current checkout."""

from pathlib import Path

from lu_machinery.lint import state_path_literals


def test_current_tree_passes(monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    assert state_path_literals.main() == 0


def test_new_store_literals_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "lu_machinery"
    package.mkdir()
    source = 'store = root / "batch_state"\narchive = root / \'batch_state/tasks/archive\'\n'
    (package / "state_paths.py").write_text(source, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "fixture.py").write_text(source, encoding="utf-8")
    assert state_path_literals.main() == 0
    capsys.readouterr()

    (package / "caller.py").write_text(source, encoding="utf-8")
    assert state_path_literals.main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "lu_machinery/caller.py:1: use a path builder from lu_machinery.state_paths",
        "lu_machinery/caller.py:2: use a path builder from lu_machinery.state_paths",
        "State path literal lint: 2 violation(s).",
    ]
