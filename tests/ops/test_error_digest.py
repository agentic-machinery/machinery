"""Exercise the daily report with a journalctl fake and a private task store."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery import state_paths
from lu_machinery.dispatcher import store
from lu_machinery.ops import error_digest as digest

_DAY = "2025-02-01"
_START = 1738368000000000
_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def journal(tmp_path, monkeypatch):
    task_store = state_paths.tasks_dir(tmp_path)
    task_store.mkdir(parents=True)
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


def test_signature_ignores_ids_times_paths(journal):
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
    github = "gh" + "p_" + "Ab2C" * 9
    slack_tail = "abcdefghijklmnopqrstuvwxyz"
    slack = "xox" + "b-" + "1234567890-2345678901-" + slack_tail
    directory = journal([
        _entry(PRIORITY="3", MESSAGE="padding " * 24 + github),
        _entry(PRIORITY="3", MESSAGE=f"provider refused {slack}"),
    ])
    assert _run(directory) == 0
    report = (directory / f"error-digest-{_DAY}.json").read_text()
    assert "Ab2C" not in report
    assert slack_tail not in report


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
    uid = str(os.getuid())
    directory = journal(
        [_entry(PRIORITY="6"), _entry(PRIORITY="5", _TRANSPORT="syslog"),
         _entry("tagged.service", PRIORITY="6"),
         _entry("child.service", PRIORITY="3", _TRANSPORT="journal", _COMM="systemd",
                _SYSTEMD_UNIT=f"user@{uid}.service", _UID=uid,
                MESSAGE_ID=digest._UNIT_FAILURE, USER_UNIT="writer.service"),
         _entry("init.scope", PRIORITY="6", _TRANSPORT="journal", _COMM="systemd",
                _SYSTEMD_UNIT=f"user@{uid}.service", _UID=uid,
                MESSAGE_ID=digest._UNIT_FAILURE, USER_UNIT="tagged.service")],
        coverage=[_entry("tagged.service", PRIORITY="7")],
    )
    assert _run(directory) == 0
    path = directory / f"error-digest-{_DAY}.json"
    first = path.read_bytes()
    report = json.loads(first)
    assert report["journal"]["units"] == [
        {"family": "child.service", "coverage": "severity unproven", "error": 1},
        {"family": "tagged.service", "coverage": "tagged", "error": 1, "warning": 0, "unparsed": 0},
        {"family": "writer.service", "coverage": "severity unproven"},
    ]
    assert [item["group"] for item in report["kinds"] if item["template"] == "unit-failed"] == [["tagged.service"]]
    journal(head=_START + 1)
    assert _run(directory) == 0
    assert path.read_bytes() == first


@pytest.fixture
def destinations(tmp_path, monkeypatch):
    binding = tmp_path / "host.json"
    binding.write_text(json.dumps({"schema": "machinery.sandbox-state.v1", "state_root": str(tmp_path)}))
    monkeypatch.setenv("LU_HOST_CONTEXT", str(binding))
    fake = tmp_path / "fake-digest-destination"
    fake.write_text(
        f"#!{sys.executable}\n"
        """\
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["journal", "add"]:
    assert args[2:6] == ["--project", "fleet", "--type", "note"]
    assert args[6] == "--headline" and args[8] == "--body" and args[10] == "--source-key"
    assert len(args) == 12 and len(args[9]) <= 2000
    assert os.environ["LU_INBOX_WORKER"] == "devops"
    target, key, body = "journal", args[11], args[7] + "\\n" + args[9]
    if os.environ.get("DIGEST_FAKE_JOURNAL_FAIL"):
        sys.exit(1)
else:
    assert args[:12] == [
        "channel", "publish", "cto", "-", "--sender", "devops",
        "--recipient", "claude-cto", "--kind", "proposal", "--idempotency-key", args[-1],
    ]
    assert len(args) == 12
    target, key, body = "fleet", args[11], sys.stdin.read()
    if os.environ.get("DIGEST_FAKE_FLEET_FAIL") == "before_write":
        sys.exit(1)
path = Path(os.environ["DIGEST_FIXTURE"], target + "-writes.json")
writes = json.loads(path.read_text()) if path.exists() else {}
if key in writes:
    if target == "fleet":
        assert writes[key] == body
else:
    writes[key] = body
    path.write_text(json.dumps(writes))
if target == "fleet" and os.environ.get("DIGEST_FAKE_FLEET_FAIL") == "after_write":
    sys.exit(1)
"""
    )
    fake.chmod(0o700)
    monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(fake))
    monkeypatch.setenv("LU_ERROR_DIGEST_FLEET_CLI", str(fake))

    def writes(target):
        path = tmp_path / f"{target}-writes.json"
        return json.loads(path.read_text()) if path.exists() else {}

    return writes


def _publish(directory: Path, day: str = _DAY) -> subprocess.CompletedProcess[str]:
    env = {
        name: os.environ[name] for name in (
            "PATH", "DIGEST_FIXTURE", "DIGEST_FIXTURE_EXIT", "DIGEST_FAKE_FLEET_FAIL",
            "DIGEST_FAKE_JOURNAL_FAIL", "LU_HOST_CONTEXT",
            "LU_DRIVER_STATE_CLI", "LU_ERROR_DIGEST_FLEET_CLI",
        ) if name in os.environ
    }
    return subprocess.run(
        [sys.executable, "-m", "lu_machinery.ops.error_digest",
         "--day", day, "--report-dir", str(directory), "--publish", "--sender", "devops"],
        cwd=_REPO, env=env, text=True, capture_output=True, check=False, timeout=20,
    )


def _proposal_days(proposals: dict[str, str]) -> list[str]:
    """Each Fleet key is bound to its day, sender and body; return the days in order."""
    days = [key.split(":")[2] for key in proposals]
    expected = [digest._proposal_key(day, "devops", body) for day, body in zip(days, proposals.values(), strict=True)]
    assert list(proposals) == expected
    return days


def test_second_run_same_day_adds_no_entry_or_message(journal, destinations, monkeypatch):
    """A rerun can duplicate the journal entry or Fleet proposal."""
    directory = journal([_entry(PRIORITY="3"), _entry(PRIORITY="3")])
    monkeypatch.setenv("DIGEST_FIXTURE_EXIT", "1")
    failed = _publish(directory)
    assert failed.returncode == 1
    assert "journal: unavailable (journalctl_refused)" in failed.stderr
    assert destinations("journal") == destinations("fleet") == {}
    monkeypatch.delenv("DIGEST_FIXTURE_EXIT")
    first = _publish(directory)
    assert first.returncode == 0, first.stderr
    notes, proposals = destinations("journal"), destinations("fleet")
    key = f"error-digest:{_DAY}"
    assert list(notes) == [key] and _proposal_days(proposals) == [_DAY]
    (fleet_key,) = proposals
    assert notes[key].startswith(f"Error digest {_DAY}: 2 errors, 0 warnings, 1 kinds (1 new); 0 units unproven")
    assert "Full table: error-digest-2025-02-01.json" in notes[key]
    assert "| writer.service | 2 | 0 | 0 | tagged |" in notes[key]
    kind = digest.signature("journal", "writer.service", "error", "worker failed")
    assert f"{kind} (2): writer.service error - worker failed" in proposals[fleet_key]
    ledger = directory / "error-digest-kinds.v1.json"
    first_ledger = ledger.read_bytes()
    assert json.loads(first_ledger)[kind]["proposal_day"] == _DAY
    second = _publish(directory)
    assert second.returncode == 0, second.stderr
    assert destinations("journal") == notes
    assert destinations("fleet") == proposals
    assert ledger.read_bytes() == first_ledger
    print(f"run 1: +{len(notes)} journal entry, +{len(proposals)} Fleet proposal")
    print(f"run 2: +{len(destinations('journal')) - len(notes)} journal entries, "
          f"+{len(destinations('fleet')) - len(proposals)} Fleet proposals")

    next_day = "2025-02-02"
    journal([_entry(PRIORITY="4", MESSAGE="request stalled", __REALTIME_TIMESTAMP=str(_START + 86_400_000_000))])
    monkeypatch.setenv("DIGEST_FAKE_JOURNAL_FAIL", "1")
    failed = _publish(directory, next_day)
    assert failed.returncode == 1
    assert "journal write: unavailable (write_refused)" in failed.stderr
    assert destinations("journal") == notes
    next_key = f"error-digest:{next_day}"
    proposals = destinations("fleet")
    assert _proposal_days(proposals) == [_DAY, next_day] and next(iter(proposals)) == fleet_key

    monkeypatch.delenv("DIGEST_FAKE_JOURNAL_FAIL")
    recovered = _publish(directory, next_day)
    assert recovered.returncode == 0, recovered.stderr
    assert "1 warnings, 1 kinds (1 new)" in destinations("journal")[next_key]
    assert destinations("fleet") == proposals


def test_failed_publish_leaves_ledger_unchanged(journal, destinations, monkeypatch):
    """A kind can be marked proposed before Fleet accepts the message."""
    directory = journal([_entry(PRIORITY="3")])
    directory.mkdir()
    ledger = directory / "error-digest-kinds.v1.json"
    ledger.write_text("{}\n")
    previous = ledger.read_bytes()
    monkeypatch.setenv("DIGEST_FAKE_FLEET_FAIL", "before_write")
    failed = _publish(directory)
    assert failed.returncode == 1
    assert "Fleet publish: unavailable (write_refused)" in failed.stderr
    assert ledger.read_bytes() == previous
    assert destinations("fleet") == {}
    key = f"error-digest:{_DAY}"
    state = directory / ".error-digest-publish.v2.json"
    attempt = json.loads(state.read_bytes())["attempt"]
    assert (attempt["day"], attempt["sender"]) == (_DAY, "devops")
    assert f"Fleet proposal for {_DAY} (1 kinds) not confirmed" in destinations("journal")[key]

    # An ambiguous completion on a later run must still leave the kind ledger
    # unchanged. The pending proposal is resent under its own key, never a fresh one.
    next_day = "2025-02-02"
    journal([_entry(PRIORITY="3", MESSAGE="new failure", __REALTIME_TIMESTAMP=str(_START + 86_400_000_000))])
    monkeypatch.setenv("DIGEST_FAKE_FLEET_FAIL", "after_write")
    ambiguous = _publish(directory, next_day)
    assert ambiguous.returncode == 1
    assert ledger.read_bytes() == previous
    fleet = destinations("fleet")
    assert _proposal_days(fleet) == [_DAY]
    (fleet_key,) = fleet
    assert json.loads(state.read_bytes())["attempt"] == attempt

    monkeypatch.delenv("DIGEST_FAKE_FLEET_FAIL")
    next_run = _publish(directory, next_day)
    assert next_run.returncode == 0, next_run.stderr
    next_key = f"error-digest:{next_day}"
    proposals = destinations("fleet")
    assert _proposal_days(proposals) == [_DAY, next_day]
    assert proposals[fleet_key] == fleet[fleet_key]
    next_fleet_key = list(proposals)[1]
    kind = digest.signature("journal", "writer.service", "error", "worker failed")
    new_kind = digest.signature("journal", "writer.service", "error", "new failure")
    assert new_kind in proposals[next_fleet_key] and kind not in proposals[next_fleet_key]
    rows = json.loads(ledger.read_bytes())
    assert {sig: row["proposal_day"] for sig, row in rows.items()} == {kind: _DAY, new_kind: next_day}
    assert json.loads(state.read_bytes()) == {"attempt": None}
    assert sorted(destinations("journal")) == [key, next_key]
