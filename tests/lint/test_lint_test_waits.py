"""Gate defects for lint L4: timer waits, reruns, and loose xfails."""

from __future__ import annotations

from pathlib import Path

from lu_machinery.lint.lint_test_waits import collect, evaluate, write_baseline


def _tree(tmp_path: Path, source: str) -> tuple[Path, Path]:
    root = tmp_path / "tree"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_sample.py").write_text(source, encoding="utf-8")
    return root, root / "baseline.json"


def test_new_time_sleep_in_a_test_body_fails(tmp_path: Path) -> None:
    """Defect class: a new time.sleep in a test body waits on a timer."""

    source = (
        "import threading\n"
        "import time\n"
        "\n"
        "def test_body():\n"
        "    threading.Event().wait(timeout=1)\n"
        "    time.sleep(0)\n"
    )
    root, baseline = _tree(tmp_path, source)
    write_baseline(baseline, [])
    violations = evaluate(root, baseline)
    assert len(violations) == 1
    assert violations[0].startswith("tests/test_sample.py: new sleep in test_body occurrence 0 ")
    assert "tests.wait_helpers.wait_until" in violations[0]
    assert "stale" not in violations[0]


def test_timer_polling_loop_fails(tmp_path: Path) -> None:
    """Defect class: a while or for loop that sleeps polls on a timer."""

    source = (
        "import time\n"
        "\n"
        "def test_poll():\n"
        "    while True:\n"
        "        time.sleep(0.01)\n"
        "    for _ in range(3):\n"
        "        time.sleep(0.01)\n"
    )
    root, baseline = _tree(tmp_path, source)
    write_baseline(baseline, [])
    violations = evaluate(root, baseline)
    assert len(violations) == 2
    assert all(item.startswith("tests/test_sample.py: new timer-poll in test_poll ") for item in violations)
    assert all("tests.wait_helpers.wait_for_line" in item for item in violations)
    assert all(": new sleep " not in item for item in violations)


def test_baselined_occurrence_passes_and_second_identical_occurrence_fails(tmp_path: Path) -> None:
    """Defect class: a second identical sleep in a baselined function is a new occurrence."""

    source = (
        "import time\n"
        "\n"
        "def test_waits():\n"
        "    time.sleep(1)\n"
        "    time.sleep(1)\n"
    )
    root, baseline = _tree(tmp_path, source)
    current, fatal = collect(root)
    assert fatal == []
    sleeps = sorted((item for item in current if item.kind == "sleep"), key=lambda item: item.occurrence)
    assert [item.occurrence for item in sleeps] == [0, 1]
    assert sleeps[0].fingerprint == sleeps[1].fingerprint
    assert sleeps[0].scope == sleeps[1].scope == "test_waits"
    write_baseline(baseline, [sleeps[0]])
    violations = evaluate(root, baseline)
    assert len(violations) == 1
    assert "new sleep in test_waits occurrence 1 " in violations[0]
    assert "occurrence 0 " not in violations[0]
    assert "stale" not in violations[0]


def test_stale_baseline_row_fails(tmp_path: Path) -> None:
    """Defect class: a baseline row whose sleep was removed is stale and fails."""

    root, baseline = _tree(tmp_path, "import time\n\ndef test_clean():\n    time.sleep(1)\n")
    current, fatal = collect(root)
    assert fatal == []
    assert len(current) == 1
    write_baseline(baseline, current)
    (root / "tests" / "test_sample.py").write_text("def test_clean():\n    assert True\n", encoding="utf-8")
    violations = evaluate(root, baseline)
    assert len(violations) == 1
    assert violations[0].startswith("tests/test_sample.py: stale baseline sleep in test_clean occurrence 0 ")


def test_non_strict_xfail_fails_and_strict_xfail_passes(tmp_path: Path) -> None:
    """Defect class: xfail without strict=True conceals a failure."""

    source = (
        "import pytest\n"
        "\n"
        "@pytest.mark.xfail\n"
        "def test_loose():\n"
        "    assert False\n"
        "\n"
        "@pytest.mark.xfail(strict=True)\n"
        "def test_strict():\n"
        "    assert False\n"
    )
    root, baseline = _tree(tmp_path, source)
    write_baseline(baseline, [])
    violations = evaluate(root, baseline)
    assert len(violations) == 1
    assert "new xfail in test_loose " in violations[0]
    assert "strict=True" in violations[0]
    assert "test_strict" not in violations[0]
