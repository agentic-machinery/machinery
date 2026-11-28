"""Exercise the daily report with a journalctl fake and a private task store."""

import json
import os
from pathlib import Path

import pytest

from lu_machinery.dispatcher import store
from lu_machinery.ops import error_digest as digest

_DAY = "2025-02-01"
_START = 1738368000000000


@pytest.fixture
def journal(tmp_path, monkeypatch):
    task_store = tmp_path / "tasks"
    task_store.mkdir()
    monkeypatch.setattr(store, "tasks_dir", lambda: task_store)
    monkeypatch.setenv("LU_TASKS_DIR", str(task_store))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DIGEST_FIXTURE", str(tmp_path))
    executable = tmp_path / "journalctl"
    executable.write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        '  *--lines=+1*) cat "$DIGEST_FIXTURE/head";;\n'
        '  *--since=*) cat "$DIGEST_FIXTURE/day"; exit "${DIGEST_FIXTURE_EXIT:-0}";;\n'
        '  *) cat "$DIGEST_FIXTURE/coverage";;\n'
        'esac\n'
    )
    executable.chmod(0o700)

    def prepare(rows=(), *, coverage=(), head=_START - 1):
        for name, data in (
            ("head", [{"__REALTIME_TIMESTAMP": str(head)}]),
            ("day", rows),
            ("coverage", coverage),
        ):
            (tmp_path / name).write_text("".join(json.dumps(row) + "\n" for row in data))
        return tmp_path / "reports"

    return prepare


def _entry(unit="writer.service", **fields):
    return {
        "__REALTIME_TIMESTAMP": str(_START),
        "_SYSTEMD_USER_UNIT": unit,
        "_TRANSPORT": "stdout",
        "MESSAGE": "worker failed",
        **fields,
    }


def _run(directory: Path) -> int:
    return digest.main(["--day", _DAY, "--report-dir", str(directory)])


def test_signature_ignores_ids_times_paths():
    """Volatile details and later traceback lines can split one kind."""
    first = (
        "2025-02-01T03:44:55.666+00:00 Failed #41 uuid b69be3b9-cad2-45aa-863e-8e83db33104d "
        "at /var/log/c59bda71/run with 20ms from 192.0.2.5:8100 via https://example.org/#41 "
        "for alice@example.org\ntraceback one"
    )
    second = (
        "2025-02-02 01:00:05Z Failed #91 uuid f7b9e9d9-5ad2-4cb0-bae2-0f1c9ec4a257 "
        "at ~/tmp/run with 5ms from [2001:db8::f]:443 via https://example.net/two "
        "for bob@example.net\ntraceback two"
    )
    templates = [digest.normalise(message, set(), set()) for message in (first, second)]
    assert templates == [
        "<ts> Failed #<n> uuid <uuid> at <path> with <n>ms from <addr> via <url> for <email>"
    ] * 2
    assert [digest.signature("journal", "writer.service", "error", template) for template in templates] == [
        "e872923fc557"
    ] * 2


def test_task_ids_normalised_by_exact_set():
    """Normalize known whole identities and keep nearby words and unknown IDs."""
    task = "stalled-work-b69be3b9-cad2-45aa-863e-8e83db33104d"
    assert digest.normalise(
        f"triage failed: ({task}), pending-work. pending-work-retry pre-pending-work pending-writer codex codexbar",
        {task, "pending-work"},
        {"codex"},
    ) == (
        "triage failed: (<task>), <task>. pending-work-retry pre-pending-work pending-writer <agent> codexbar"
    )


def test_missing_priority_is_unparsed_not_info(journal):
    """Missing and unparseable journal priority must remain visible."""
    directory = journal([
        _entry(), _entry(PRIORITY=["3", "6"]), _entry(PRIORITY="6"), _entry(PRIORITY="4"),
    ])
    assert _run(directory) == 0
    report = json.loads((directory / f"error-digest-{_DAY}.json").read_bytes())
    assert report["totals"] == {"error": 0, "warning": 1, "unparsed": 2}
    assert report["journal"]["units"] == [
        {"family": "writer.service", "coverage": "tagged", "error": 0, "warning": 1, "unparsed": 2},
    ]


def test_partial_window_fails_closed(journal, monkeypatch, capsys):
    """A late head or a failed reader must not persist counts for an incomplete day."""
    directory = journal(head=_START + 1)
    assert _run(directory) == 1
    assert "journal: partial (head_after_window_start)" in capsys.readouterr().err
    assert not directory.exists()

    journal([_entry(PRIORITY="4")])
    monkeypatch.setenv("DIGEST_FIXTURE_EXIT", "1")
    assert _run(directory) == 1
    assert "journal: unavailable (journalctl_refused)" in capsys.readouterr().err
    assert not directory.exists()


def test_untagged_unit_reported_unproven_not_zero(journal):
    """Child syslog priority cannot prove tagging for a unit's stdout."""
    directory = journal(
        [_entry(PRIORITY="6"), _entry(PRIORITY="5", _TRANSPORT="syslog"),
         _entry("tagged.service", PRIORITY="6")],
        coverage=[_entry("tagged.service", PRIORITY="7")],
    )
    assert _run(directory) == 0
    path = directory / f"error-digest-{_DAY}.json"
    first = path.read_bytes()
    assert json.loads(first)["journal"]["units"] == [
        {"family": "tagged.service", "coverage": "tagged", "error": 0, "warning": 0, "unparsed": 0},
        {"family": "writer.service", "coverage": "severity unproven"},
    ]
    journal(head=_START + 1)
    assert _run(directory) == 0
    assert path.read_bytes() == first
