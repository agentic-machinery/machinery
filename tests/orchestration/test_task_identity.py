from __future__ import annotations

import json
from copy import deepcopy
import pytest
from lu_machinery.orchestration import task_identity
import io
import os
import subprocess
import sys
from pathlib import Path
from lu_machinery.orchestration import job_host_exec as jh
from tests.helpers.product_tree import configured_roots as configured_roots

def _identity(*, issue: int | None = 5295) -> dict:
    return task_identity.build_identity(
        repository=task_identity.DEFAULT_REPOSITORY,
        stream_epic=4707,
        stream_epic_url=None,
        github_issue_number=issue,
        github_issue_url=None,
        semantic_title="Repair fleet rollover task identity",
        task_family="infra-harness",
        role="orchestrator",
        predecessor_task_id="predecessor-1",
        replacement_task_id=None,
        lineage_id="lineage-task-identity",
        generation=2,
        terminal_goal="merge",
    )


def _fresh_identity(**overrides) -> dict:
    return task_identity.build_identity(**{
        "repository": task_identity.DEFAULT_REPOSITORY,
        "stream_epic": 4707,
        "stream_epic_url": None,
        "github_issue_number": 10143,
        "github_issue_url": None,
        "semantic_title": "Initialize fresh task lifecycle",
        "task_family": "infra-harness",
        "role": "driver",
        "fresh_task_id": "native-thread-10143",
        "terminal_goal": "merge",
        "lifecycle_state": "active",
        **overrides,
    })


def test_fresh_identity_validates_without_rollover_ancestry() -> None:
    identity = _crash_roundtrip(_fresh_identity())
    assert task_identity.validate_identity(identity) == identity
    assert list(task_identity._IDENTITY_VALIDATOR.iter_errors(identity)) == []
    assert identity["origin"] == "fresh"
    assert identity["task_id"] == "native-thread-10143"
    assert identity["lifecycle_state"] == "active"
    assert set(identity).isdisjoint({"lineage_id", "generation", "predecessor_task_id", "replacement_task_id"})
    assert set(identity["carriers"].values()) == {identity["visible_title"]}


@pytest.mark.parametrize("task_id", ["", " ", " native-thread", "native thread", "native\nthread", "native\x00thread", "native\x7fthread", 7])
def test_fresh_builder_rejects_malformed_native_task_ids(task_id) -> None:
    with pytest.raises(ValueError):
        _fresh_identity(fresh_task_id=task_id)


@pytest.mark.parametrize("field,value", [
    ("lineage_id", "lineage-real"), ("lineage_id", ""),
    ("predecessor_task_id", "thread-old"), ("replacement_task_id", "thread-new"),
    ("generation", 1), ("generation", 0),
    ("lifecycle_state", "confirmed"), ("lifecycle_state", ""), ("lifecycle_state", None),
    ("lifecycle_state", "prepared"),
    ("legacy_fallback", True), ("terminal_goal", "unknown"),
])
def test_fresh_builder_rejects_rollover_and_legacy_fields(field, value) -> None:
    with pytest.raises(ValueError):
        _fresh_identity(**{field: value})


@pytest.mark.parametrize("field,value", [
    ("origin", None), ("origin", "rollover"), ("task_id", None), ("task_id", ""),
    ("task_id", "native thread"), ("task_id", "native\x00thread"), ("task_id", "native-thread\n"),
    ("lineage_id", None), ("lineage_id", ""), ("lineage_id", "lineage-real"),
    ("predecessor_task_id", None), ("replacement_task_id", None),
    ("generation", 0), ("generation", 1), ("lifecycle_state", "resumed"),
])
def test_fresh_schema_rejects_malformed_and_mixed_envelopes(field, value) -> None:
    identity = _fresh_identity()
    identity[field] = value
    assert list(task_identity._IDENTITY_VALIDATOR.iter_errors(identity))
    with pytest.raises(ValueError, match="schema violation"):
        task_identity.validate_identity(identity)


@pytest.mark.parametrize("field,value", [
    ("lineage_id", None), ("lineage_id", ""), ("lineage_id", "Bad-Lineage"),
    ("predecessor_task_id", None), ("predecessor_task_id", ""),
    ("generation", None), ("generation", 0), ("generation", True),
    ("origin", "fresh"), ("task_id", "native-thread"), ("lifecycle_state", "active"),
])
def test_rollover_schema_still_rejects_invalid_identity(field, value) -> None:
    identity = _identity()
    identity[field] = value
    with pytest.raises(ValueError, match="schema violation"):
        task_identity.validate_identity(identity)


def test_fresh_identity_cannot_resume_an_unrelated_rollover() -> None:
    identity = _fresh_identity()
    with pytest.raises(ValueError, match="no rollover title transition"):
        task_identity.mark_resumed(identity, _transition(identity), replacement_task_id=identity["task_id"])


def _transition(identity: dict, harness: str = "codex-app") -> dict:
    return task_identity.new_title_transition(
        harness=harness,
        visible_title_value=identity["visible_title"],
        prepared_at="2026-07-16T08:00:00Z",
    )


def _crash_roundtrip(value: dict) -> dict:
    return json.loads(json.dumps(value))


def test_semantic_title_survives_every_native_title_boundary_and_crash() -> None:
    identity = _crash_roundtrip(_identity())
    transition = _crash_roundtrip(_transition(identity))
    title = "#5295 — Repair fleet rollover task identity"
    assert identity["visible_title"] == title

    identity, transition = task_identity.bind_replacement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        evidence="exact native create receipt",
        now="2026-07-16T08:01:00Z",
    )
    identity, transition = _crash_roundtrip(identity), _crash_roundtrip(transition)
    identity, transition = task_identity.record_title_acknowledgement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        succeeded=True,
        evidence="set_thread_title acknowledged",
        error="",
        now="2026-07-16T08:02:00Z",
    )
    identity, transition = _crash_roundtrip(identity), _crash_roundtrip(transition)
    identity, transition = task_identity.record_title_readback(
        identity,
        transition,
        replacement_task_id="replacement-2",
        observed_title=title,
        succeeded=True,
        evidence="exact native readback",
        error="",
        now="2026-07-16T08:03:00Z",
    )
    identity, transition = _crash_roundtrip(identity), _crash_roundtrip(transition)
    identity = task_identity.mark_resumed(
        identity,
        transition,
        replacement_task_id="replacement-2",
    )
    identity = _crash_roundtrip(identity)
    identity = task_identity.mark_confirmed(
        identity,
        transition,
        replacement_task_id="replacement-2",
    )

    assert identity["semantic_title"] == "Repair fleet rollover task identity"
    assert identity["visible_title"] == title
    assert identity["lifecycle_state"] == "confirmed"
    assert set(identity["carriers"].values()) == {title}


def test_title_acknowledgement_without_readback_is_not_reconciled() -> None:
    identity = _identity()
    transition = _transition(identity)
    identity, transition = task_identity.bind_replacement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        evidence="exact binding",
        now="2026-07-16T08:01:00Z",
    )
    identity, transition = task_identity.record_title_acknowledgement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        succeeded=True,
        evidence="adapter acknowledgement",
        error="",
        now="2026-07-16T08:02:00Z",
    )

    with pytest.raises(ValueError, match="acknowledgement without exact readback"):
        task_identity.assert_title_ready(
            identity,
            transition,
            replacement_task_id="replacement-2",
        )


def test_incorrect_native_title_readback_fails_closed() -> None:
    identity = _identity()
    transition = _transition(identity)
    identity, transition = task_identity.bind_replacement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        evidence="exact binding",
        now="2026-07-16T08:01:00Z",
    )
    identity, transition = task_identity.record_title_readback(
        identity,
        transition,
        replacement_task_id="replacement-2",
        observed_title="Replacement task",
        succeeded=True,
        evidence="native readback",
        error="wrong visible title",
        now="2026-07-16T08:02:00Z",
    )

    assert identity["lifecycle_state"] == "title_failed"
    assert transition["readback_receipt"]["exact_match"] is False
    with pytest.raises(ValueError, match="without exact readback"):
        task_identity.assert_title_ready(
            identity,
            transition,
            replacement_task_id="replacement-2",
        )


def test_incorrect_replacement_id_fails_closed_at_every_title_gate() -> None:
    identity = _identity()
    transition = _transition(identity)
    identity, transition = task_identity.bind_replacement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        evidence="exact binding",
        now="2026-07-16T08:01:00Z",
    )

    with pytest.raises(ValueError, match="exact persisted binding"):
        task_identity.bind_replacement(
            identity,
            transition,
            replacement_task_id="wrong-replacement",
            evidence="wrong binding",
            now="2026-07-16T08:02:00Z",
        )
    with pytest.raises(ValueError, match="exact binding"):
        task_identity.record_title_acknowledgement(
            identity,
            transition,
            replacement_task_id="wrong-replacement",
            succeeded=True,
            evidence="wrong acknowledgement",
            error="",
            now="2026-07-16T08:02:00Z",
        )


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        "Replacement task",
        "Resume rollover",
        "Codex continuation",
        "Generation 2",
        "lineage-abc123",
        "rollover-abc123",
        "019f6a34-df56-4951-9384-c160b627789b",
    ],
)
def test_blank_generic_and_runtime_identifier_titles_are_rejected(title: str) -> None:
    with pytest.raises(ValueError):
        task_identity.validate_semantic_title(title)


def test_unsupported_adapter_records_honest_carrier_fallback() -> None:
    identity = _identity(issue=None)
    transition = _transition(identity, harness="claude")
    identity, transition = task_identity.bind_replacement(
        identity,
        transition,
        replacement_task_id="claude-task-2",
        evidence="dispatch runtime task binding",
        now="2026-07-16T08:01:00Z",
    )

    receipt = transition["fallback_receipt"]
    assert identity["visible_title"] == "infra-harness — Repair fleet rollover task identity"
    assert receipt["native_mutation_supported"] is False
    assert receipt["attempted"] is False
    assert receipt["carriers"] == list(task_identity.FALLBACK_CARRIERS)
    task_identity.assert_title_ready(
        identity,
        transition,
        replacement_task_id="claude-task-2",
    )


@pytest.mark.parametrize(
    ("harness", "native_supported"),
    [
        ("codex-app", True),
        ("claude", False),
        ("gemini", False),
        ("agy", False),
        ("cursor", False),
        ("opencode", False),
    ],
)
def test_every_harness_uses_the_shared_identity_contract(harness: str, native_supported: bool) -> None:
    identity = _identity()
    transition = _transition(identity, harness=harness)

    assert task_identity.validate_identity(identity) == identity
    assert task_identity.validate_title_transition(transition, identity) == transition
    assert transition["native_title_supported"] is native_supported
    assert transition["visible_title"] == identity["visible_title"]


def test_legacy_packet_receives_deterministic_identity_backfill() -> None:
    legacy = {
        "schema_version": 2,
        "agent": "claude",
        "lineage_id": "lineage-legacy-packet",
        "active": {"thread_id": "legacy-predecessor"},
        "replacement": {
            "rollover_id": "rollover-legacy",
            "lineage_id": "lineage-legacy-packet",
            "generation": 3,
            "status": "pending_start",
            "prepared_at": "2026-07-15T08:00:00Z",
            "display": {"title": "Generation 3"},
        },
    }

    first, migrated = task_identity.backfill_legacy_identity(
        legacy,
        agent="claude",
        repository=task_identity.DEFAULT_REPOSITORY,
        now="2026-07-16T08:00:00Z",
    )
    second, migrated_again = task_identity.backfill_legacy_identity(
        deepcopy(first),
        agent="claude",
        repository=task_identity.DEFAULT_REPOSITORY,
        now="2026-07-17T08:00:00Z",
    )

    assert migrated is True
    assert migrated_again is False
    assert second["replacement"]["identity"] == first["replacement"]["identity"]
    assert first["replacement"]["identity"]["visible_title"] == (
        "thread-rollover — Recover predecessor task context"
    )
    assert first["replacement"]["identity"]["migration"] == {
        "source": "legacy-v2-deterministic-fallback",
        "legacy_fallback": True,
    }
    assert first["replacement"]["identity"]["terminal_goal"] == "unknown"
    assert first["replacement"]["title_transition"]["harness"] == "claude-legacy"
    assert first["replacement"]["title_transition"]["state"] == "awaiting_replacement_binding"


def test_title_boundary_retries_are_idempotent() -> None:
    identity = _identity()
    transition = _transition(identity)
    identity, transition = task_identity.bind_replacement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        evidence="binding",
        now="2026-07-16T08:01:00Z",
    )
    bound = (deepcopy(identity), deepcopy(transition))
    assert task_identity.bind_replacement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        evidence="retry",
        now="2026-07-16T09:01:00Z",
    ) == bound

    identity, transition = task_identity.record_title_acknowledgement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        succeeded=True,
        evidence="acknowledgement",
        error="",
        now="2026-07-16T08:02:00Z",
    )
    acknowledged = (deepcopy(identity), deepcopy(transition))
    assert task_identity.record_title_acknowledgement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        succeeded=True,
        evidence="retry",
        error="",
        now="2026-07-16T09:02:00Z",
    ) == acknowledged

    identity, transition = task_identity.record_title_readback(
        identity,
        transition,
        replacement_task_id="replacement-2",
        observed_title=identity["visible_title"],
        succeeded=True,
        evidence="readback",
        error="",
        now="2026-07-16T08:03:00Z",
    )
    reconciled = (deepcopy(identity), deepcopy(transition))
    assert task_identity.record_title_readback(
        identity,
        transition,
        replacement_task_id="replacement-2",
        observed_title=identity["visible_title"],
        succeeded=True,
        evidence="retry",
        error="",
        now="2026-07-16T09:03:00Z",
    ) == reconciled


def test_native_title_failure_can_retry_to_durable_success() -> None:
    identity = _identity()
    transition = _transition(identity)
    identity, transition = task_identity.bind_replacement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        evidence="binding",
        now="2026-07-16T08:01:00Z",
    )
    identity, transition = task_identity.record_title_acknowledgement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        succeeded=False,
        evidence="first adapter attempt",
        error="transient failure",
        now="2026-07-16T08:02:00Z",
    )
    assert identity["lifecycle_state"] == "title_failed"
    assert transition["state"] == "title_mutation_failed"

    identity, transition = task_identity.record_title_acknowledgement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        succeeded=True,
        evidence="retry acknowledged",
        error="",
        now="2026-07-16T08:03:00Z",
    )
    identity, transition = task_identity.record_title_readback(
        identity,
        transition,
        replacement_task_id="replacement-2",
        observed_title=identity["visible_title"],
        succeeded=True,
        evidence="exact retry readback",
        error="",
        now="2026-07-16T08:04:00Z",
    )
    reconciled = (deepcopy(identity), deepcopy(transition))

    assert identity["lifecycle_state"] == "title_ready"
    assert transition["state"] == "title_reconciled"
    assert transition["mutation_receipt"]["succeeded"] is True
    assert transition["readback_receipt"]["exact_match"] is True

    assert task_identity.record_title_acknowledgement(
        identity,
        transition,
        replacement_task_id="replacement-2",
        succeeded=False,
        evidence="late duplicate failure",
        error="transient adapter response",
        now="2026-07-16T10:02:00Z",
    ) == reconciled
    assert task_identity.record_title_readback(
        identity,
        transition,
        replacement_task_id="replacement-2",
        observed_title="late stale value",
        succeeded=False,
        evidence="late duplicate failure",
        error="transient adapter response",
        now="2026-07-16T10:03:00Z",
    ) == reconciled


@pytest.fixture()
def _clear_occupancy_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(jh.ENV_OCCUPANCY_HOST, raising=False)
    monkeypatch.delenv(jh.ENV_RUNTIME_INITIATOR, raising=False)
    monkeypatch.delenv(jh.ENV_RUNTIME_INITIATOR_SOURCE, raising=False)


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_cli_help_runs_without_pythonpath(tmp_path: Path) -> None:
    script = Path(jh.__file__).resolve()
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Examples:" in proc.stdout
    assert "Exit codes:" in proc.stdout


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_build_ssh_argv_is_batchmode() -> None:
    argv = jh.build_ssh_argv("job-alias", "true")
    assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12"]
    assert argv[5] == "job-alias"


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_fails_closed_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(jh.ENV_HOST, raising=False)
    monkeypatch.delenv(jh.ENV_HOST_FALLBACK, raising=False)
    monkeypatch.delenv(jh.ENV_REPO, raising=False)
    monkeypatch.delenv(jh.ENV_TEACHER_HOST, raising=False)
    monkeypatch.delenv(jh.ENV_TEACHER_REPO, raising=False)
    monkeypatch.delenv(jh.ENV_DISPATCH_SSH, raising=False)
    with pytest.raises(ValueError, match="is required"):
        jh.job_dispatch_host()
    with pytest.raises(ValueError, match="is required"):
        jh.job_dispatch_repo()
    with pytest.raises(ValueError, match="is required"):
        jh.ssh_alias_for_host_id("host-job")
    with pytest.raises(ValueError, match="is required"):
        jh.ssh_alias_for_host_id("host-teacher")
    with pytest.raises(ValueError, match="is required"):
        jh.repo_for_host_id("host-teacher")


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_main_uses_ssh_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "ssh.args"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {args_file}\ncat > {args_file}.stdin\nexit 0\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv(jh.ENV_TEACHER_HOST, "teacher-alias")
    monkeypatch.setenv(jh.ENV_TEACHER_REPO, "/remote/repo")
    rc = jh.main(["--host-id", "host-teacher", "--", ".venv/bin/python", "lu_machinery/dispatch.py", "dispatch"])
    assert rc == 0
    recorded = args_file.read_text(encoding="utf-8")
    script = (tmp_path / "ssh.args.stdin").read_text(encoding="utf-8")
    assert "BatchMode=yes" in recorded
    assert "teacher-alias" in recorded
    assert "bash -s" in recorded
    assert "cd /remote/repo" in script
    assert "lu_machinery/dispatch.py" in script
    assert f"export {jh.ENV_ALLOW_NOTEBOOK}=1" in script


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_main_dispatch_preserves_session_initiator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "ssh.args"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {args_file}\ncat > {args_file}.stdin\nexit 0\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv(jh.ENV_HOST, "job-alias")
    monkeypatch.setenv(jh.ENV_REPO, "/remote/repo")
    monkeypatch.setenv("SESSION_HANDOFF_AGENT", "cursor")
    rc = jh.main(
        [
            "--host-id",
            "host-job",
            "--",
            ".venv/bin/python",
            "lu_machinery/dispatch.py",
            "dispatch",
            "--agent",
            "codex",
            "--task-id",
            "cf",
        ]
    )
    assert rc == 0
    script = (tmp_path / "ssh.args.stdin").read_text(encoding="utf-8")
    assert "--initiator" in script
    assert "cursor" in script
    assert f"export {jh.ENV_RUNTIME_INITIATOR}=" in script
    assert f"export {jh.ENV_RUNTIME_INITIATOR_SOURCE}=" in script


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_main_help_contract() -> None:
    help_text = jh.build_parser().format_help()
    assert "Execute a command on a VPS worker checkout over BatchMode SSH." in help_text
    assert "do not use it to start a second Monitor" in help_text
    assert "Examples:" in help_text
    assert "Outputs:" in help_text
    assert "Exit codes:" in help_text
    assert "Related:" in help_text
    assert "--prompt-file" in help_text
    assert "Issue: #7062" in help_text


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_forward_dispatch_inlines_prompt_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "ssh.args"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {args_file}\ncat > {args_file}.stdin\nexit 0\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    prompt = tmp_path / "brief.md"
    prompt.write_text("keep curriculum/l2-uk-en in the worktree\n", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv(jh.ENV_HOST, "job-alias")
    monkeypatch.setenv(jh.ENV_REPO, "/remote/repo")
    rc = jh.forward_dispatch(
        host_id="host-job",
        argv=[
            "lu_machinery/dispatch.py",
            "dispatch",
            "--agent",
            "codex",
            "--task-id",
            "cf",
            "--prompt-file",
            str(prompt),
        ],
    )
    assert rc == 0
    recorded = args_file.read_text(encoding="utf-8")
    script = (tmp_path / "ssh.args.stdin").read_text(encoding="utf-8")
    assert str(prompt) not in recorded
    assert "keep curriculum/l2-uk-en in the worktree" not in recorded
    assert "--prompt-file" in script
    assert "/tmp/lu-dispatch-prompt.XXXXXX" in script
    assert "$LU_DISPATCH_PROMPT_0" in script
    assert "\n--prompt\n-\n" not in script
    assert "keep curriculum/l2-uk-en in the worktree" not in script


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_forward_dispatch_rewrites_prompt_dash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "ssh.args"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {args_file}\ncat > {args_file}.stdin\nexit 0\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv(jh.ENV_HOST, "job-alias")
    monkeypatch.setenv(jh.ENV_REPO, "/remote/repo")

    class _Stdin:
        buffer = io.BytesIO(b"touch wiki/pages.md\n")

    monkeypatch.setattr(jh.sys, "stdin", _Stdin())
    rc = jh.forward_dispatch(
        host_id="host-job",
        argv=[
            "lu_machinery/dispatch.py",
            "dispatch",
            "--agent",
            "codex",
            "--task-id",
            "cf",
            "--prompt",
            "-",
        ],
    )
    assert rc == 0
    recorded = args_file.read_text(encoding="utf-8")
    script = (tmp_path / "ssh.args.stdin").read_text(encoding="utf-8")
    assert "--prompt-file" in script
    assert "/tmp/lu-dispatch-prompt.XXXXXX" in script
    assert "$LU_DISPATCH_PROMPT_0" in script
    assert "\n--prompt\n-\n" not in script
    assert "touch wiki/pages.md" not in recorded


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_forward_dispatch_rejects_notebook_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(jh.ENV_HOST, "job-alias")
    monkeypatch.setenv(jh.ENV_REPO, "/remote/repo")
    with pytest.raises(ValueError, match="--cwd"):
        jh.forward_dispatch(
            host_id="host-job",
            argv=[
                "lu_machinery/dispatch.py",
                "dispatch",
                "--agent",
                "codex",
                "--task-id",
                "cf",
                "--cwd",
                str(tmp_path),
            ],
        )


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_forward_dispatch_strips_notebook_worktree_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "ssh.args"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {args_file}\ncat > {args_file}.stdin\nexit 0\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv(jh.ENV_HOST, "job-alias")
    monkeypatch.setenv(jh.ENV_REPO, "/remote/repo")
    local_wt = tmp_path / "wt"
    local_wt.mkdir()
    rc = jh.forward_dispatch(
        host_id="host-job",
        argv=[
            "lu_machinery/dispatch.py",
            "dispatch",
            "--agent",
            "codex",
            "--task-id",
            "cf",
            "--worktree",
            str(local_wt),
        ],
    )
    assert rc == 0
    recorded = args_file.read_text(encoding="utf-8")
    script = (tmp_path / "ssh.args.stdin").read_text(encoding="utf-8")
    assert str(local_wt) not in recorded
    assert "--worktree" in script


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_forward_dispatch_missing_ssh_is_transport_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv(jh.ENV_HOST, "job-alias")
    monkeypatch.setenv(jh.ENV_REPO, "/remote/repo")
    with pytest.raises(jh.SshTransportError):
        jh.forward_dispatch(
            host_id="host-job",
            argv=["lu_machinery/dispatch.py", "dispatch", "--agent", "codex", "--task-id", "cf"],
        )


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
@pytest.mark.parametrize(
    ("mode", "expected_rc"),
    [("success", 0), ("failure", 7), ("signal", 143)],
)
def test_remote_payload_script_cleans_private_files_on_exit_failure_and_signal(
    tmp_path: Path, mode: str, expected_rc: int
) -> None:
    """The remote trap must remove each 0600 payload file in every exit path."""
    remote_python = tmp_path / ".venv" / "bin" / "python"
    remote_python.parent.mkdir(parents=True)
    remote_python.symlink_to(sys.executable)
    remote_dispatch = tmp_path / "lu_machinery" / "dispatch.py"
    remote_dispatch.parent.mkdir()
    observed_path = tmp_path / "payload-path"
    remote_dispatch.write_text(
        "import os\n"
        "import signal\n"
        "import stat\n"
        "import sys\n"
        "from pathlib import Path\n"
        "payload = Path(sys.argv[sys.argv.index('--prompt-file') + 1])\n"
        "mode = stat.S_IMODE(payload.stat().st_mode)\n"
        "Path(os.environ['LU_TEST_PAYLOAD_PATH']).write_text(f'{payload}\\n{mode:o}', encoding='utf-8')\n"
        "if os.environ['LU_TEST_PAYLOAD_MODE'] == 'signal':\n"
        "    os.kill(os.getppid(), signal.SIGTERM)\n"
        "sys.exit(7 if os.environ['LU_TEST_PAYLOAD_MODE'] == 'failure' else 0)\n",
        encoding="utf-8",
    )
    prompt = tmp_path / "brief.md"
    prompt.write_text("private body\n", encoding="utf-8")
    rest, payloads = jh.materialize_local_dispatch_argv(["dispatch", "--prompt-file", str(prompt)])
    script = jh._build_remote_dispatch_script(
        argv=rest,
        remote_repo=str(tmp_path),
        payloads=payloads,
        extra_exports=[
            f"export LU_TEST_PAYLOAD_PATH={observed_path}",
            f"export LU_TEST_PAYLOAD_MODE={mode}",
        ],
    )
    rendered = script.decode("utf-8")
    assert "umask 077" in rendered
    assert "mktemp /tmp/lu-dispatch-prompt.XXXXXX" in rendered
    assert "trap _on_exit EXIT" in rendered
    assert "trap '_on_signal 15' TERM" in rendered
    assert "private body" not in rendered
    completed = subprocess.run(
        ["bash", "-s"],
        input=script,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == expected_rc, completed.stderr.decode("utf-8")
    payload_name, payload_mode = observed_path.read_text(encoding="utf-8").splitlines()
    payload_path = Path(payload_name)
    assert int(payload_mode, 8) & 0o077 == 0
    assert not payload_path.exists()


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_main_missing_ssh_dispatch_returns_255(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv(jh.ENV_HOST, "job-alias")
    monkeypatch.setenv(jh.ENV_REPO, "/remote/repo")
    rc = jh.main(
        [
            "--host-id",
            "host-job",
            "--",
            ".venv/bin/python",
            "lu_machinery/dispatch.py",
            "dispatch",
            "--agent",
            "codex",
            "--task-id",
            "cf",
        ]
    )
    assert rc == 255


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
@pytest.mark.parametrize(
    "entrypoint",
    [
        "lu_machinery/dispatch.py",
        "/local/checkout/scripts/dispatch.py",
        "lu_machinery/dispatch.py",
        "/local/checkout/scripts/dispatch.py",
    ],
)
def test_forward_dispatch_propagates_run_nonce_in_argv_and_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    """#7168: forward_dispatch passes --run-nonce in remote argv and exports LU_RUNTIME_RUN_NONCE."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "ssh.args"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {args_file}\ncat > {args_file}.stdin\nexit 0\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv(jh.ENV_HOST, "job-alias")
    monkeypatch.setenv(jh.ENV_REPO, "/remote/repo")
    rc = jh.forward_dispatch(
        host_id="host-job",
        argv=[entrypoint, "dispatch", "--agent", "codex", "--task-id", "cf"],
        run_nonce="nonce-fwd-9876",
    )
    assert rc == 0
    script = (tmp_path / "ssh.args.stdin").read_text(encoding="utf-8")
    assert f".venv/bin/python lu_machinery/{Path(entrypoint).name} dispatch" in script
    assert "/local/checkout" not in script
    assert "--run-nonce" in script
    assert "nonce-fwd-9876" in script
    assert f"export {jh.ENV_RUNTIME_RUN_NONCE}=" in script


@pytest.mark.usefixtures('configured_roots', '_clear_occupancy_pin')
@pytest.mark.reads_content
def test_job_host_exec_main_refusal_names_both_recovery_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """#7230: job_host_exec.py CLI prints the refusal naming recovery paths and exits rc 2 on config error."""
    monkeypatch.delenv(jh.ENV_HOST, raising=False)
    monkeypatch.delenv(jh.ENV_HOST_FALLBACK, raising=False)
    rc = jh.main(["--host-id", "host-job", "--", "lu_machinery/dispatch.py", "dispatch", "--task-id", "t-1"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "❌ VPS forward configuration refusal for host-job:" in captured.err
    assert "LU_JOB_DISPATCH_HOST" in captured.err
    assert "LU_ALLOW_NOTEBOOK_DISPATCH=1" in captured.err
