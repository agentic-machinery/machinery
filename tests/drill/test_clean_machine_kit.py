"""Static and unit checks for the public clean-machine drill kit (drill/clean-machine).

The drill itself downloads releases and runs for minutes; these tests check what must hold
without running it: the row table, the receipt a row writes, the origin oracle, and that the
public kit carries nothing host-specific.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[2] / "drill" / "clean-machine"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"clean_machine_{name}", KIT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def drill():
    return _load("drill")


@pytest.fixture(scope="module")
def rows():
    return json.loads((KIT / "rows.json").read_text())


def _probe_names() -> set[str]:
    tree = ast.parse((KIT / "probes.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and getattr(decorator.func, "id", None) == "probe"
                ):
                    names.add(decorator.args[0].value)
    return names


def test_every_row_is_classified_once_and_runs_a_known_probe(rows, drill):
    """Defect class: a consumer row silently missing from the drill, counted twice, left
    unclassified, or pointing at a probe that does not exist (a row that can never pass)."""
    ids = [row["id"] for row in rows["rows"]]
    assert len(ids) == len(set(ids)) == rows["denominator"]
    assert all(re.fullmatch(r"R\d{3}", row_id) for row_id in ids)
    assert set(rows["classes"]) == {"as-is", "mock", "cannot-run"}
    kit_probes = _probe_names()
    for row in rows["rows"]:
        assert row["class"] in rows["classes"], row
        assert row["probe"] in rows["probes"], row
        if row["class"] == "cannot-run":
            assert row.get("reason"), row
        probe = rows["probes"][row["probe"]]
        for mock in probe["mocks"]:
            assert mock in rows["mocks"], (row["id"], mock)
        if row["class"] == "mock":
            assert probe["mocks"], row
        argv = probe["argv"]
        if "{kit}/probes.py" in argv:
            assert argv[argv.index("{kit}/probes.py") + 1] in kit_probes, row
    assert set(rows["probes"]) == {row["probe"] for row in rows["rows"]}, "unused probe"
    assert drill.load_rows(KIT / "rows.json")["rows"] == rows["rows"]


def _names(tmp_path: Path) -> dict[str, str]:
    world = tmp_path / "world"
    state = world / "stubs" / "state"
    state.mkdir(parents=True)
    (state / "answers.json").write_text("{}")
    names = {
        key: str(tmp_path / key)
        for key in (
            "home",
            "product",
            "worktree",
            "stubs",
            "venv_bin",
            "kit",
            "fixtures",
        )
    }
    names.update(
        world=str(world),
        stub_state=str(state),
        py=sys.executable,
        base_py=sys.executable,
        sources_url="http://127.0.0.1:9",
        monitor_url="http://127.0.0.1:9",
    )
    return names


RECEIPT_KEYS = {
    "schema",
    "row",
    "class",
    "kind",
    "probe",
    "mocks",
    "phase",
    "version",
    "status",
    "failures",
    "exit_code",
    "started_utc",
    "duration_s",
    "origin",
    "stdout_tail",
    "stderr_tail",
}


def test_receipt_records_pass_fail_version_and_timing(tmp_path, drill):
    """Defect class: a receipt that omits the verdict, version or timing, or reports pass when
    the row's known answer is missing."""
    row = {"id": "R001", "kind": "test", "class": "as-is", "probe": "p"}
    passing = {
        "argv": [sys.executable, "-c", "print('known answer 42')"],
        "mocks": [],
        "expect": {"rc": 0, "stdout_re": ["answer 42"]},
    }
    failing = {
        "argv": [sys.executable, "-c", "print('other')"],
        "mocks": [],
        "expect": {"rc": 0, "stdout_re": ["answer 42"]},
    }
    out = tmp_path / "out"
    names = _names(tmp_path)
    good = drill.run_row(row, passing, names, "forward", "9.9.9", out, False, tmp_path)
    bad = drill.run_row(row, failing, names, "rollback", "9.9.8", out, False, tmp_path)
    for receipt, phase in ((good, "forward"), (bad, "rollback")):
        assert set(receipt) == RECEIPT_KEYS
        assert receipt["schema"] == drill.RECEIPT_SCHEMA
        assert (
            json.loads((out / "receipts" / phase / "R001.json").read_text()) == receipt
        )
        assert isinstance(receipt["duration_s"], float) and re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", receipt["started_utc"]
        )
        assert receipt["origin"]["method"] == "none"
    assert (good["status"], good["version"], good["failures"]) == ("pass", "9.9.9", [])
    assert (bad["status"], bad["version"]) == ("fail", "9.9.8")
    assert bad["failures"] == ["stdout lacks /answer 42/"]


def test_origin_oracle_flags_machinery_code_outside_the_release(tmp_path, drill):
    """Defect class: accidental dependence, a checkout or copy of machinery code loaded instead
    of the installed release, passing the drill unnoticed."""
    site = tmp_path / "venv" / "site-packages"
    (site / "lu_machinery").mkdir(parents=True)
    checkout = tmp_path / "checkout" / "lu_machinery"
    checkout.mkdir(parents=True)
    trace = tmp_path / "trace"
    trace.write_text(
        f'10 openat(AT_FDCWD, "{site}/lu_machinery/__init__.py", O_RDONLY|O_CLOEXEC) = 3\n'
        f'10 openat(AT_FDCWD, "{checkout}/dispatch.py", O_RDONLY|O_CLOEXEC) = 4\n'
        f'10 openat(AT_FDCWD, "{checkout}/missing.py", O_RDONLY) = -1 ENOENT (No such file or directory)\n'
        f'11 execve("{tmp_path}/checkout/agents_extensions/hook.sh", ["hook.sh"], 0x0 /* 3 vars */) = 0\n'
        f'10 openat(AT_FDCWD, "{tmp_path}/product/state.json", O_RDONLY) = 5\n'
    )
    violations, loaded = drill.origin_findings(trace, site)
    assert loaded == 1
    assert violations == [
        str(checkout / "dispatch.py"),
        str(tmp_path / "checkout" / "agents_extensions" / "hook.sh"),
    ]


HOST_PATTERNS = {
    "home or user directory path": re.compile(r"/home/[a-z]|/Users/|/root/"),
    "host user config directory": re.compile(
        r"\.config/learn-ukrainian|ops-state|\.local/lib/lu-"
    ),
    "private repository": re.compile(r"infra-private"),
    "deployed unit name": re.compile(r"\blu-[a-z0-9-]+\.(service|timer)\b"),
    "non-loopback IPv4 address": re.compile(
        r"\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b"
    ),
    "email address": re.compile(r"[\w.+-]+@(?!example\.invalid\b)[\w-]+\.[\w.]+"),
    "secret-looking token": re.compile(
        r"\b(?:ghp|gho|github_pat|sk|xox[abp])[-_][A-Za-z0-9_]{16,}"
    ),
}


def _tracked_kit_files() -> list[str]:
    root = KIT.parents[1]
    listed = subprocess.run(
        ["git", "ls-files", "--", "drill/clean-machine"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(str((root / name).relative_to(KIT)) for name in listed)


@pytest.mark.parametrize("path", _tracked_kit_files())
def test_kit_holds_no_host_specific_strings(path):
    """Defect class: the public kit or its extracted fixtures leaking a host's paths, private
    repositories, deployed unit names, addresses, accounts or credentials (the repository is public)."""
    text = (KIT / path).read_text()
    found = {
        label: pattern.findall(text)[:3]
        for label, pattern in HOST_PATTERNS.items()
        if pattern.search(text)
    }
    assert not found, f"{path}: {found}"


def test_extracted_task_records_keep_only_scrubbed_fields():
    """Defect class: a fixture extract carrying private task data (prompts, paths, ids, accounts)
    instead of the scrubbed status, mode, agent and model."""
    records = json.loads((KIT / "fixtures" / "task-records.json").read_text())
    assert len(records) >= 3
    assert all(
        set(record) == {"status", "mode", "agent", "model"} for record in records
    )
    assert (
        sum(path.stat().st_size for path in (KIT / "fixtures").iterdir()) < 1024 * 1024
    )
