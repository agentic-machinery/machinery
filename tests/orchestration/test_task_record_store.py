"""The hot/archive task-record layout shared by every reader (#8625)."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest

from lu_machinery.orchestration import task_record_store as store


def test_iter_task_records_yields_hot_then_archive(tmp_path):
    tasks = tmp_path / "tasks"
    (tasks / "archive").mkdir(parents=True)
    for path in (tasks / "b.json", tasks / "a.json", tasks / "archive" / "a.json", tasks / "archive" / "c.json"):
        path.write_text(json.dumps({"task_id": path.stem, "schema_version": 2}), encoding="utf-8")
    (tasks / "a.result").write_text("reply", encoding="utf-8")
    (tasks / "nested.json").mkdir()  # a directory is never a record
    # Dispatcher receipts sit beside records and are not candidates. A foreign JSON file still is.
    for name in ("a.write-boundary.json", "a.sandbox.json", "a.tool-results.json"):
        (tasks / name).write_text("{}", encoding="utf-8")
    (tasks / "archive" / "c.write-boundary.json").write_text("{}", encoding="utf-8")
    (tasks / "receipt.json").write_text("[]", encoding="utf-8")

    names = [str(path.relative_to(tasks)) for path in store.iter_task_records(tasks, include_archive=True)]
    assert names == ["a.json", "b.json", "receipt.json", "archive/a.json", "archive/c.json"]
    hot = [path.name for path in store.iter_task_records(tasks, include_archive=False)]
    assert hot == ["a.json", "b.json", "receipt.json"]
    assert list(store.iter_task_records(tmp_path / "missing", include_archive=True)) == []


def test_inventory_enumeration_does_not_read_payloads(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    (tasks / "archive").mkdir(parents=True)
    for path in (tasks / "current.json", tasks / "archive" / "prior.json"):
        path.write_text(json.dumps({"schema_version": 2, "task_id": path.stem}))
    original_open = Path.open

    def deny_payload(path, *args, **kwargs):
        if path.parent in {tasks, tasks / "archive"}:
            raise AssertionError("candidate enumeration must not read task payloads")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_payload)
    assert [str(path.relative_to(tasks)) for path in store.iter_task_records(tasks, include_archive=True)] == [
        "current.json", "archive/prior.json"
    ]


def test_locate_prefers_the_hot_record(tmp_path):
    tasks = tmp_path / "tasks"
    archived = store.archived_task_record_path(tasks, "codex/task")
    archived.parent.mkdir(parents=True)
    archived.write_text("{}", encoding="utf-8")
    assert archived.name == "codex_task.json"
    assert store.locate_task_record(tasks, "codex/task") == archived
    hot = store.task_record_path(tasks, "codex/task")
    hot.write_text("{}", encoding="utf-8")
    assert store.locate_task_record(tasks, "codex/task") == hot
    assert store.locate_task_record(tasks, "absent") is None

    names = (
        "codex_task.json", "codex_task.20261023T091234123Z.archived.json",
        "codex_task.20261023T091234Z.42.archived.json",
        "codex_task.20261023T091234Z.42.archived.20261023T101234Z.7.archived.json",
        "codex_task-extra.json", "codex_task.bad.archived.json", "codex_task.result",
    )
    expected = tuple(sorted(names[:4]))
    assert store.candidate_task_record_names("codex/task", names) == expected
    assert store.candidate_task_record_names("codex\\task", names) == expected
    # Pure filtering does not hide an unsafe matching filesystem entry.
    hot.unlink()
    hot.mkdir()
    assert store.candidate_task_record_names("codex/task", [hot.name]) == (hot.name,)
    assert store.candidate_task_record_names("other", names) == ()


def test_a_listed_record_follows_its_own_file_across_archive_and_restore(tmp_path, monkeypatch):
    """Defect class: recovery of a moved file reads another run with the same task ID."""
    canonical = store.archived_task_record_path(tmp_path, "review")
    canonical.parent.mkdir()
    canonical.write_bytes(b'{"task_id": "review", "run_nonce": "canonical"}')
    archived = canonical.with_name("review.20261119T140000Z.archived.json")
    hot = tmp_path / archived.name
    legacy = b'{"task_id": "review", "run_nonce": "legacy"}'
    hot.write_bytes(legacy)
    for path in (canonical, hot):
        os.utime(path, ns=(1, 1))
    read_bytes = Path.read_bytes

    def archive_before_read(path):
        if path == hot:
            hot.rename(archived)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", archive_before_read)
    assert store.read_task_record_bytes(hot, tasks_dir=tmp_path) == (archived, legacy)
    assert store.locate_task_record(tmp_path, "review") == canonical

    def restore_before_read(path):
        if path == archived:
            archived.rename(hot)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", restore_before_read)
    assert store.read_task_record_bytes(archived, tasks_dir=tmp_path) == (hot, legacy)

    hot_canonical = tmp_path / canonical.name
    hot_canonical.write_bytes(b'{"task_id": "review", "run_nonce": "replacement"}')

    def collide_before_read(path):
        if path == hot_canonical:
            hot_canonical.rename(archived.with_name("review.20261119T150000Z.archived.json"))
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", collide_before_read)
    with pytest.raises(FileNotFoundError):
        store.read_task_record_bytes(hot_canonical, tasks_dir=tmp_path)


def test_relocated_result_file_points_at_the_moved_sidecar(tmp_path, monkeypatch):
    from lu_machinery.dispatcher import store as dispatch_store
    from lu_machinery.orchestration import orchestrator_control, stale_task_records

    tasks = tmp_path / "tasks"
    archived = store.archived_task_record_path(tasks, "old")
    archived.parent.mkdir(parents=True)
    hot_result = str(tasks / "old.result")
    # Sidecar not moved (never had one): the recorded path is returned unchanged.
    assert store.relocated_result_file(archived, hot_result) == hot_result
    archived.with_suffix(".result").write_text("reply", encoding="utf-8")
    assert store.relocated_result_file(archived, hot_result) == str(archived.with_suffix(".result"))
    # Hot records and empty values are never rewritten.
    assert store.relocated_result_file(tasks / "old.json", hot_result) == hot_result
    assert store.relocated_result_file(archived, None) is None

    archived.write_text(json.dumps({"task_id": "old", "run_nonce": "first", "result_file": hot_result}))
    os.utime(archived, ns=(1, 1))
    stamped = archived.with_name("old.20261119T140000Z.archived.json")
    stamped.write_text(json.dumps({"task_id": "old", "run_nonce": "second", "result_file": hot_result}))
    stamped_reply = stamped.with_suffix(".result")
    stamped_reply.write_text("second run's reply", encoding="utf-8")
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))

    for state in (dispatch_store.read("old", tasks_root=tasks), orchestrator_control.load_task_state(tasks, "old")):
        assert state["run_nonce"] == "first"
        assert Path(state["result_file"]).read_text() == "reply"

    states = dispatch_store.list_tasks(include_archive=True, strict=True)
    assert [(state["run_nonce"], Path(state["result_file"]).read_text()) for state in states] == [
        ("second", "second run's reply"), ("first", "reply"),
    ]

    stamped_reply.unlink()
    assert store.relocated_result_file(stamped, hot_result) == str(stamped_reply)
    stamped_reply.write_text("second run's reply", encoding="utf-8")
    (tasks / "old.json").write_text(json.dumps({"task_id": "old", "run_nonce": "third", "result_file": hot_result}))
    Path(hot_result).write_text("third run's reply", encoding="utf-8")

    assert stale_task_records.restore_archived(tasks, [stamped.name], apply=True)["actions"] == {"restored": 1}

    restored = tasks / stamped.name
    assert store.relocated_result_file(restored, str(stamped_reply)) == str(restored.with_suffix(".result"))

    states = dispatch_store.list_tasks(include_archive=True, strict=True)
    by_nonce = {state["run_nonce"]: Path(state["result_file"]).read_text() for state in states}
    assert by_nonce == {"first": "reply", "second": "second run's reply", "third": "third run's reply"}


def test_v2_record_under_another_name_raises_identity_mismatch_with_its_content(tmp_path):
    """Defect class: a reader cannot tell a copied record from a renamed one, or a copy reads as valid."""
    path = tmp_path / "worker.review-wait.json"
    record = {"schema_version": 2, "task_id": "worker", "status": "done"}
    with pytest.raises(store.TaskIdentityMismatch, match="has invalid task identity") as caught:
        store.parse_task_record(path, json.dumps(record).encode())
    assert isinstance(caught.value, ValueError) and caught.value.record == record
    with pytest.raises(ValueError, match="has invalid task identity") as non_string:
        store.parse_task_record(tmp_path / "worker.json", json.dumps({**record, "task_id": 7}).encode())
    assert not isinstance(non_string.value, store.TaskIdentityMismatch)


def test_blank_v2_named_record_is_damaged_not_foreign(tmp_path):
    """Defect class: an empty or NUL-filled v2-named record read as foreign (#12219)."""
    for raw in (b"", b" \n\t", b"\x00" * 64, b"\xef\xbb\xbf\x00"):
        for name in ("worker.json", "worker.20261023T091234Z.7.archived.json"):
            path = tmp_path / name
            with pytest.raises(ValueError, match=re.escape(f"task record {path} is empty")):
                store.parse_task_record(path, raw)
    # A blank file no dispatch could have named stays foreign.
    assert store.parse_task_record(tmp_path / "not a task id.json", b"") is None


def test_foreign_filename_cannot_inject_log_lines(tmp_path, caplog):
    """Defect class: a foreign sidecar filename forges a second log entry."""
    # CR/LF, NEL, line/paragraph separators, ESC, and a literal format token.
    fragments = ("\r\n", "\u0085", "\u2028", "\u2029", "\x1b", "%(levelname)s")
    formatter = logging.Formatter("%(levelname)s %(message)s")
    for fragment in fragments:
        before = len(caplog.records)
        path = tmp_path / f"foreign{fragment}FORGED entry.json"
        assert store.parse_task_record(path, b"[]") is None
        assert len(caplog.records) == before + 1
        message = caplog.records[-1].getMessage()
        assert message == f"NON_RECORD_FILE: {str(path)!r}"
        assert not any(sep in message for sep in ("\n", "\r", "\u0085", "\u2028", "\u2029", "\x1b"))
        assert "\n" not in formatter.format(caplog.records[-1])

    # Dispatcher sidecars are not foreign. The receipt suffixes come from the writers, not a guessed pattern.
    from lu_machinery.dispatcher.agy_sandbox import RECEIPT_SUFFIX as sandbox_suffix
    from lu_machinery.dispatcher.agy_sandbox import tool_results_file
    from lu_machinery.dispatcher.write_boundary import RECEIPT_SUFFIX as boundary_suffix

    record = tmp_path / "task.json"
    sidecars = (
        record.with_suffix(boundary_suffix),
        record.with_suffix(sandbox_suffix),
        tool_results_file(record.with_suffix(".result")),
        record.with_suffix(".prompt"),
        record.with_suffix(".result"),
        record.with_suffix(record.suffix + ".lock"),
        record.with_suffix(".rules-core"),
        record.with_suffix(".launch-prompt"),
        record.with_suffix(".log"),
        record.with_suffix(".diag"),
        Path(str(record.with_suffix(".diag")) + ".prev"),
        record.with_suffix(".snapshots"),
        tmp_path / "task.inputs",
        tmp_path / "task.artifacts",
        tmp_path / "task.evidence",
    )
    payload = b'{"schema": 1, "task_id": "task", "status": "ran"}'
    for path in sidecars:
        before = len(caplog.records)
        assert store.parse_task_record(path, payload) is None
        assert len(caplog.records) == before
    before = len(caplog.records)
    assert store.parse_task_record(tmp_path / "receipt.json", payload) is None
    assert len(caplog.records) == before + 1
    assert "receipt.json" in caplog.records[-1].getMessage()
