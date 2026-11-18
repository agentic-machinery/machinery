"""Gate defects for the scripts/ mypy ratchet (#12642)."""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from lu_machinery.lint import mypy_ratchet as ratchet
from lu_machinery.lint.mypy_ratchet import (
    MypyError,
    collect_errors,
    evaluate,
    load_entries,
    prune_baseline,
    write_baseline,
)

_CONFIG = """\
[tool.mypy]
python_version = "3.12"
show_error_codes = true
"""


def _tree(tmp_path: Path, source: str) -> tuple[Path, Path]:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "pyproject.toml").write_text(_CONFIG, encoding="utf-8")
    (root / "mod.py").write_text(source, encoding="utf-8")
    return root, root / "baseline.json"


def _check(root: Path, baseline: Path) -> list[str]:
    return evaluate(
        root,
        baseline,
        ["mod.py"],
        config_file=root / "pyproject.toml",
        incremental=False,
    )


def test_new_type_error_does_not_pass_the_gate(tmp_path: Path) -> None:
    """A type error absent from the baseline fails the gate."""
    root, baseline = _tree(tmp_path, "def answer() -> str:\n    return 1\n")
    write_baseline(baseline, [])
    violations = _check(root, baseline)
    assert violations
    assert any(": new " in item for item in violations)


def test_fixed_error_left_in_the_baseline_does_not_pass(tmp_path: Path) -> None:
    """A baseline entry mypy no longer reports is stale and fails."""
    root, baseline = _tree(tmp_path, "def answer() -> str:\n    return 'ok'\n")
    write_baseline(baseline, [MypyError("mod.py", "return-value", "fixed error")])
    violations = _check(root, baseline)
    assert any("stale baseline" in item for item in violations)


def test_line_move_does_not_change_baseline_identity(tmp_path: Path) -> None:
    """Inserting lines above an error keeps the same identity, so the gate stays green."""
    root, baseline = _tree(tmp_path, "def answer() -> str:\n    return 1\n")
    before, before_fatal = collect_errors(
        root, ["mod.py"], config_file=root / "pyproject.toml", incremental=False
    )
    assert before_fatal == []
    assert [error.code for error in before] == ["return-value"]
    (root / "mod.py").write_text("\n\n\ndef answer() -> str:\n    return 1\n", encoding="utf-8")
    after, after_fatal = collect_errors(
        root, ["mod.py"], config_file=root / "pyproject.toml", incremental=False
    )
    assert after_fatal == []
    assert after == before
    write_baseline(baseline, after)
    assert _check(root, baseline) == []


def test_baseline_growth_against_landed_main_is_refused(tmp_path: Path) -> None:
    """Defect class: the shrink guard lets a committed baseline grow past origin/main.

    The baseline that landed on origin/main is the ceiling: the same multiset passes,
    and one more entry fails as growth against origin/main.
    """

    message = 'Variable "lu_machinery.pkg.mod.Base" is not valid as a type'
    root = tmp_path / "tree"
    lint = root / "lu_machinery" / "lint"
    lint.mkdir(parents=True)
    baseline = lint / "mypy_baseline.json"
    landed = MypyError("lu_machinery/pkg/mod.py", "valid-type", message)
    write_baseline(baseline, [landed])
    git = ["git", "-C", str(root), "-c", "user.name=Ratchet Fixture", "-c", "user.email=ratchet@example.invalid"]
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "landed"]):
        subprocess.run([*git, *args], check=True, capture_output=True, timeout=30)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True, timeout=30)

    assert ratchet.shrink_violations(root, baseline, load_entries(baseline)) == []

    write_baseline(baseline, [landed, MypyError("lu_machinery/pkg/mod.py", "misc", "new error")])
    grown = ratchet.shrink_violations(root, baseline, load_entries(baseline))
    assert any("may only shrink" in item and "origin/main" in item for item in grown)


# The ratchet builds its own mypy config from the tree's pyproject, which rejects keys it does not know.
_TREE_CONFIG = '[tool.mypy]\npython_version = "3.12"\n'
_BAD = "def answer() -> str:\n    return 1\n"
_GOOD = "def answer() -> str:\n    return 'ok'\n"


def _scripts_tree(tmp_path: Path, source: str) -> tuple[Path, Path, MypyError]:
    """A product-shaped tree: scripts/mod.py, the config, and a baseline holding the real error."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    (root / "pyproject.toml").write_text(_TREE_CONFIG, encoding="utf-8")
    (root / "scripts" / "mod.py").write_text(source, encoding="utf-8")
    real = MypyError("scripts/mod.py", "return-value", 'Incompatible return value type (got "int", expected "str")')
    baseline = root / "baseline.json"
    write_baseline(baseline, [real])
    return root, baseline, real


def test_complete_analysis_prunes_stale_entries_atomically(tmp_path: Path) -> None:
    """Defect class: a fixed error stays in the baseline, or the rewrite drops a live one or leaves a temp file."""
    root, baseline, real = _scripts_tree(tmp_path, _BAD)
    stale = MypyError("scripts/gone.py", "misc", "fixed long ago")
    write_baseline(baseline, [real, stale])
    mode = baseline.stat().st_mode

    assert prune_baseline(root, baseline) == (1, [])

    assert load_entries(baseline) == [real]
    assert baseline.stat().st_mode == mode
    assert sorted(path.name for path in baseline.parent.glob(".baseline.json.*")) == []
    assert prune_baseline(root, baseline) == (0, [])  # already minimal: nothing to write


def test_new_error_refuses_the_prune_and_leaves_the_baseline_untouched(tmp_path: Path) -> None:
    """Defect class: pruning stale entries also bakes a new error into the baseline."""
    root, baseline, _real = _scripts_tree(tmp_path, _BAD)
    stale = MypyError("scripts/gone.py", "misc", "fixed long ago")
    write_baseline(baseline, [stale])
    before = baseline.read_bytes()

    removed, refusals = prune_baseline(root, baseline)

    assert removed == 0
    assert any("new return-value" in item for item in refusals)
    assert baseline.read_bytes() == before


def test_reintroduced_error_fails_after_it_was_pruned(tmp_path: Path) -> None:
    """Defect class: a pruned entry's error comes back and passes as if still accepted."""
    root, baseline, _real = _scripts_tree(tmp_path, _GOOD)
    assert prune_baseline(root, baseline) == (1, [])
    assert load_entries(baseline) == []

    (root / "scripts" / "mod.py").write_text(_BAD, encoding="utf-8")
    violations = evaluate(root, baseline, ["scripts"], config_file=None, incremental=False)
    assert any(": new return-value" in item for item in violations)


def _remove_config(root: Path, _baseline: Path) -> None:
    (root / "pyproject.toml").unlink()


def _other_mypy(_root: Path, baseline: Path) -> None:
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["mypy"] = "0.0.1"
    baseline.write_text(json.dumps(payload), encoding="utf-8")


def _remove_tree(root: Path, _baseline: Path) -> None:
    (root / "scripts" / "mod.py").unlink()
    (root / "scripts").rmdir()


def _break_syntax(root: Path, _baseline: Path) -> None:
    (root / "scripts" / "mod.py").write_text("def answer(:\n", encoding="utf-8")


@pytest.mark.parametrize("damage", [_remove_config, _other_mypy, _remove_tree, _break_syntax])
def test_incomplete_analysis_never_prunes(tmp_path: Path, damage: Callable[[Path, Path], None]) -> None:
    """Defect class: a missing config or tree, another mypy, or a failed run reads as 'no errors' and wipes debt."""
    root, baseline, _real = _scripts_tree(tmp_path, _GOOD)
    damage(root, baseline)
    before = baseline.read_bytes()

    removed, refusals = prune_baseline(root, baseline)

    assert removed == 0
    assert refusals
    assert baseline.read_bytes() == before


def test_cli_prune_takes_no_paths_and_check_mode_never_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Defect class: a path list prunes on a partial census, or CI's check run rewrites the tracked baseline."""
    root, baseline, _real = _scripts_tree(tmp_path, _GOOD)
    before = baseline.read_bytes()
    base = ["--root", str(root), "--baseline", str(baseline), "--no-incremental"]

    with pytest.raises(SystemExit) as refused:
        ratchet.main([*base, "--write-baseline", "scripts/mod.py"])
    assert refused.value.code == 2
    assert baseline.read_bytes() == before

    assert ratchet.main(base) == 1  # the entry is stale: check mode fails and names the fix
    assert "--write-baseline" in capsys.readouterr().err
    assert baseline.read_bytes() == before

    assert ratchet.main([*base, "--write-baseline"]) == 0
    assert load_entries(baseline) == []
    assert importlib.metadata.version("mypy") == json.loads(baseline.read_text(encoding="utf-8"))["mypy"]
