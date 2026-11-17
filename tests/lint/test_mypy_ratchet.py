"""Gate defects for the scripts/ mypy ratchet (#12642)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lu_machinery.lint import mypy_ratchet as ratchet
from lu_machinery.lint.mypy_ratchet import MypyError, collect_errors, evaluate, load_entries, write_baseline

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
