"""The lint rejects a new literal store path and accepts the current checkout."""

from pathlib import Path

from lu_machinery.lint import state_path_literals


def test_current_tree_passes(monkeypatch):
    # CI imports from the built wheel after removing the source package.
    monkeypatch.chdir(Path(state_path_literals.__file__).resolve().parents[2])
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
    legacy_line = 'legacy = root / "batch_state"'
    legacy_file = package / "legacy.py"
    monkeypatch.setattr(state_path_literals, "EXISTING_LINES", (("legacy.py", legacy_line),))
    legacy_file.write_text(legacy_line + "\n", encoding="utf-8")
    assert state_path_literals.main() == 0
    capsys.readouterr()

    legacy_file.unlink()
    assert state_path_literals.main() == 1
    assert capsys.readouterr().out.splitlines() == [
        f"lu_machinery/legacy.py: remove stale state path allowance for {legacy_line}",
        "State path literal lint: 1 violation(s).",
    ]
    monkeypatch.setattr(state_path_literals, "EXISTING_LINES", ())

    (package / "caller.py").write_text(source, encoding="utf-8")
    assert state_path_literals.main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "lu_machinery/caller.py:1: use a path builder from lu_machinery.state_paths",
        "lu_machinery/caller.py:2: use a path builder from lu_machinery.state_paths",
        "State path literal lint: 2 violation(s).",
    ]
