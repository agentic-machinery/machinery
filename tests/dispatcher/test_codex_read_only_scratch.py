"""A dispatched Codex read-only task gets a per-task writable scratch; the checkout stays read-only."""



import os
import shutil
import subprocess
import sys
from pathlib import Path
import pytest
from lu_machinery.agent_runtime.read_only_tmp import create_dispatch_lease, discard_dispatch_lease, dispatch_lease
from lu_machinery.dispatcher.environment import child_env, probe_env, task_scratch_path
from lu_machinery.dispatcher.providers import load
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.dispatcher import write_boundary
from tests.helpers.product_tree import configured_roots as configured_roots
import json
import re
from types import SimpleNamespace
from unittest.mock import Mock
from lu_machinery.dispatcher import agy_sandbox
from lu_machinery.dispatcher import checks
from lu_machinery.dispatcher import cli
from lu_machinery.dispatcher import git
from lu_machinery.dispatcher import providers
from lu_machinery.dispatcher import store
from lu_machinery.dispatcher import worker
from lu_machinery.dispatcher.providers import agy
from lu_machinery.dispatcher.providers import codex
from tests.agent_runtime.product_roots import agent_runtime_roots as agent_runtime_roots
from tests.helpers import current_sol

CURRENT_SOL = current_sol()

TASK_ID = "review-demo-r1"


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_RUNTIME_TMP_BASE_ROOT", str(tmp_path / "runtime"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    return checkout


def spec_for(worktree, mode="read-only"):
    prompt = worktree.parent / "prompt.txt"
    prompt.write_text("Review the change")
    return TaskSpec(TASK_ID, "codex", "explicit-model", None, mode, worktree, prompt, worktree.parent / "final.txt")


def test_scratch_is_present_private_and_outside_the_checkout(worktree):
    lease = create_dispatch_lease(TASK_ID, worktree)
    assert lease.is_dir()
    assert lease.stat().st_mode & 0o777 == 0o700
    assert not lease.is_relative_to(worktree.resolve()) and not worktree.resolve().is_relative_to(lease)
    assert lease == dispatch_lease(TASK_ID).resolve()
    assert child_env(spec_for(worktree))["TMPDIR"] == str(lease)
    with pytest.raises(FileExistsError):
        create_dispatch_lease(TASK_ID, worktree)  # one task's scratch is never shared or reused
    assert discard_dispatch_lease(TASK_ID) is None
    assert not lease.exists()
    # A symlinked namespace would place the writable scratch wherever it points.
    namespace = lease.parent
    shutil.rmtree(namespace)
    elsewhere = namespace.parent / "elsewhere"
    elsewhere.mkdir()
    namespace.symlink_to(elsewhere)
    with pytest.raises(ValueError, match="symlink"):
        create_dispatch_lease(TASK_ID, worktree)
    assert not any(elsewhere.iterdir())
    # Cleanup never deletes through it either: a same-named directory at the target survives.
    survivor = elsewhere / lease.name
    survivor.mkdir()
    (survivor / "keep.txt").write_text("kept")
    assert "symlink" in discard_dispatch_lease(TASK_ID)
    assert (survivor / "keep.txt").read_text() == "kept"


def test_discard_removes_the_lease_where_it_was_created_after_the_scratch_root_moves(worktree, tmp_path, monkeypatch):
    """Defect class: settlement resolved the lease root again, so a changed scratch root made it look in another
    directory, report nothing and leak the lease (#100 R80)."""
    created = dispatch_lease(TASK_ID)
    create_dispatch_lease(TASK_ID, worktree)
    monkeypatch.setenv("LU_RUNTIME_TMP_BASE_ROOT", str(tmp_path / "elsewhere"))
    assert discard_dispatch_lease(TASK_ID, str(created)) is None
    assert not created.exists()
    # A recorded path that is not this task's lease is refused, never deleted.
    other = created.parent / "codex-read-only-other"
    other.mkdir(parents=True)
    assert "not this task's lease" in discard_dispatch_lease(TASK_ID, str(other))
    assert other.is_dir()


def test_checkout_stays_read_only_and_only_the_scratch_is_writable(worktree, monkeypatch):
    spec = spec_for(worktree)
    lease = dispatch_lease(TASK_ID)
    invocation = load("codex").build(spec, child_env(spec))
    # The common boundary's syscall tests prove mount enforcement. This dispatch contract binds
    # Codex to that read-only mode and its own scratch rather than starting an incompatible inner sandbox.
    monkeypatch.setattr(write_boundary.shutil, "which", lambda *args, **kwargs: str(Path(sys.executable).resolve()))
    argv = write_boundary.launcher(spec, invocation, worktree.parent / "task.json")
    assert argv[argv.index("--mode") + 1] == "read-only"
    assert argv[argv.index("--runtime") + 1] == str(lease)
    # The launcher projects a task-local CODEX_HOME beside that lease, linking only the selected account's login.
    selected = Path(invocation.env.get("CODEX_HOME") or Path(invocation.env["HOME"]) / ".codex")
    assert argv[argv.index("--codex-auth") + 1] == str(selected / "auth.json")
    assert "--dangerously-bypass-approvals-and-sandbox" in invocation.argv
    assert invocation.argv[invocation.argv.index("--disable") + 1] == "apps"
    assert invocation.env["TMPDIR"] == str(lease)
    # A write task's TMPDIR is its own task scratch, which settlement removes (CLEANUP-WORKFILES-1118).
    write = spec_for(worktree, "workspace-write")
    assert child_env(write)["TMPDIR"] == child_env(write)["LU_TASK_SCRATCH"] == str(task_scratch_path(TASK_ID))
    # A provider check before the scratch exists (the OpenRouter endpoint probe) keeps the caller's TMPDIR.
    assert probe_env(write).get("TMPDIR") == os.environ.get("TMPDIR")
    assert "permissions=" not in " ".join(load("codex").build(write, child_env(write)).argv)


def test_socket_created_by_a_plugin_lands_under_the_scratch(worktree):
    spec = spec_for(worktree)
    lease = create_dispatch_lease(TASK_ID, worktree)
    # The test base can exceed the sockaddr_un limit, so bind by a relative name from inside the directory.
    child = (
        "import os, socket, tempfile\n"
        "directory = tempfile.mkdtemp(prefix='plugin-')\n"
        "os.chdir(directory)\n"
        "socket.socket(socket.AF_UNIX).bind('plugin.sock')\n"
        "print(os.path.realpath('plugin.sock'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", child],
        env={**child_env(spec), "PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).is_relative_to(lease)
    assert not any(worktree.iterdir())


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(git, "resolve_repo", lambda _: (tmp_path, dict(key="public", github="example/public")))
    monkeypatch.setattr(git, "worktree_plan", lambda *a: (tmp_path, "codex/schema", "a" * 40))
    monkeypatch.setattr(git, "create_worktree", Mock())
    monkeypatch.setattr(checks, "check_resources", lambda _: None)
    start = Mock(side_effect=lambda spec, record, prompt, timeout: store.create(record, prompt))
    monkeypatch.setattr(worker, "start", start)
    # The checkout is tmp_path itself, so the scratch under the test's scratch root would nest in it.
    monkeypatch.setattr(worker, "create_dispatch_lease", lambda *a: None)
    monkeypatch.setattr(agy_sandbox, "unavailable", lambda: None)
    monkeypatch.setattr(agy_sandbox, "launcher", lambda spec, argv: argv)
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object","required":["verdict"]}')

    def invoke(path="schema.json", agent="sol", expected=0, review=False, review_profile="code", task_id="schema"):
        model = {"agy": "gemini-4-neon-high", "claude": "claude-opus-5-5"}.get(agent, CURRENT_SOL)
        args = ["dispatch", "--agent", agent, "--model", model, "--task-id", task_id, "--prompt", "Review"]
        if agent == "agy" and review_profile is not None:
            args += ["--review-profile", review_profile]
        if path is not None:
            args += ["--output-schema", str(path)]
        if review:
            args += ["--require-review-verdict"]
        assert cli.main(args) == expected
        if expected:
            start.assert_not_called()
            git.create_worktree.assert_not_called()
            assert not (tmp_path / "tasks").exists()
            return None
        return start.call_args.args[0], store.read(task_id)

    return SimpleNamespace(invoke=invoke, schema=schema)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("agent", ["sol", "agy"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_schema_loss_record_and_real_codex_argv(dispatch, absolute, agent):
    spec, record = dispatch.invoke(dispatch.schema if absolute else "schema.json", agent=agent)
    assert record["output_schema_path"] == str(dispatch.schema)
    assert record["output_schema_sha256"] == store.digest(dispatch.schema.read_bytes())
    provider = providers.load(spec.agent)
    argv = provider.build(spec, {}).argv
    if agent == "agy":
        assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object", "required": ["verdict"]}
    else:
        assert argv[argv.index("--output-schema") + 1] == str(dispatch.schema)
    dispatch.schema.write_text('{}')
    with pytest.raises(ValueError, match="SHA-256 changed"):
        provider.build(spec, {})


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("agent", ["cursor", "muse", "opencode"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_unsupported_schema_refused_before_launch(dispatch, capsys, agent):
    dispatch.invoke(agent=agent, expected=2)
    assert "--output-schema is supported only with the effective codex, agy, or claude agent" in capsys.readouterr().err


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("kind", ["missing", "non-json", "non-object", "invalid-schema", "symlink", "relative-escape"])
@pytest.mark.parametrize("agent", ["sol", "agy"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_unsafe_schema_refused_before_launch(dispatch, capsys, kind, agent):
    path = dispatch.schema
    if kind == "missing":
        path = path.with_name("missing.json")
    elif kind in {"non-json", "non-object", "invalid-schema"}:
        path.write_text({"non-json": "prose", "non-object": "[]", "invalid-schema": '{"type":42}'}[kind])
    else:
        outside = path.parent.parent / "outside.json"
        outside.write_text('{}')
        if kind == "symlink":
            path = path.with_name("link.json")
            path.symlink_to(outside)
        else:
            path = "../outside.json"
    dispatch.invoke(path, agent=agent, expected=2)
    assert "schema" in capsys.readouterr().err


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("agent", ["sol", "agy"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_schema_symlink_substitution_after_binding_refused(dispatch, agent):
    spec, _record = dispatch.invoke(agent=agent)
    target = dispatch.schema.with_name("target.json")
    dispatch.schema.rename(target)
    dispatch.schema.symlink_to(target)
    with pytest.raises(ValueError, match="unsafe"):
        providers.load(spec.agent).build(spec, {})


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("output,status", [('{}', "failed"), ('prose', "failed"), ('{"verdict":"APPROVE"}', "done")])
@pytest.mark.parametrize("agent", ["sol", "agy"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_invalid_schema_result_cannot_settle_done(dispatch, monkeypatch, output, status, agent):
    spec, _record = dispatch.invoke(agent=agent)
    path = store.record_path(spec.task_id)
    (path.parent / "logs").mkdir()
    spec.final_file.write_text(output)
    provider = providers.load(spec.agent)
    invocation = provider.build(spec, {})
    monkeypatch.setattr(provider, "build", lambda *a: invocation)
    monkeypatch.setattr(agy_sandbox, "verify", lambda *a: ({}, None, None))
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    monkeypatch.setattr(worker.signal, "pthread_sigmask", lambda *a: None)
    monkeypatch.setattr(git, "snapshot_delivery", lambda *a: {})
    monkeypatch.setattr(worker, "_reap_settled", lambda *a, **k: None)

    def popen(*a, **kw):
        if agent == "agy":
            try:
                payload = json.loads(output)
            except ValueError:
                payload = output
            events = [
                {"event": "init", "init": {"model": spec.model}},
                {"event": "result", "result": {"status": "SUCCESS", "structured_output": payload}},
            ]
            kw["stdout"].write(("\n".join(json.dumps(event) for event in events) + "\n").encode())
        else:
            kw["stdout"].write(b'{"type":"turn.completed"}\n')
        return SimpleNamespace(pid=None, returncode=0, wait=lambda *a, **kw: 0)

    monkeypatch.setattr(worker.write_boundary, "launcher", lambda _spec, invocation, _task: list(invocation.argv))
    monkeypatch.setattr(worker.write_boundary, "verify", lambda _task, _id, mode="workspace-write": ({"status": "test-double", "mode": mode}, None))
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    worker.supervise(path, spec.options, 10)
    settled = json.loads(path.read_text())
    assert settled["status"] == status
    if status == "failed":
        assert "structured_output_invalid" in settled["error"]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_codex_stderr_excerpt_redacts_secret_and_home_path():
    secret = "key_" + "aB09" * 16
    home_path = "/home/fixture/.codex/config.toml"
    failed = codex.parse("", f"open {home_path} failed\nwith {secret}  rejected", 1, None)
    assert failed.error is not None
    assert secret not in failed.error
    assert home_path not in failed.error
    assert "/home/" not in failed.error
    assert failed.error.startswith("codex: exited with status 1: ")
    assert len(failed.error) <= len("codex: exited with status 1: ") + 500


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("character", ["\x00", "\x1b", "\x7f", "\x85", "\u200b", "\u202e", "\u2066", "\ufeff", "\t", "\r", "\n", "\r\n"])
@pytest.mark.parametrize("placement", ["split_token", "split_key", "separated", "trailing"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_control_format_characters_removed_from_stderr_excerpt(character, placement):
    secret = "key_" + "aB09" * 16
    if placement == "split_token":
        raw = "notice" + character + secret[:8] + character + secret[8:]
    elif placement == "split_key":
        raw = "notice" + character + "PASS" + character + "WORD: " + secret[4:12]
    elif placement == "separated":
        raw = "notice" + character + "PASSWORD: " + secret[4:12] + character + "TOKEN: " + secret[4:12]
    else:
        raw = "notice " + secret + character
    parsed = codex.parse("", raw, 1, None)
    if placement == "trailing" and character in {"\t", "\r", "\n", "\r\n", "\x85"}:
        expected = "codex: exited with status 1: notice [REDACTED_SECRET]"
    else:
        expected = "codex: exited with status 1: [stderr excerpt withheld: control or format characters]"
    assert parsed.error == expected
    assert character not in parsed.error
    assert secret[:8] not in parsed.error
    assert secret[8:] not in parsed.error
    assert secret[4:12] not in parsed.error


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("signal", ["credit balance is too low", "insufficient_quota", "billing error 429"])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.usefixtures("output_schema_hooks")
def test_stderr_text_alone_cannot_mark_pool_exhausted(tmp_path, monkeypatch, signal, structured):
    from lu_machinery.dispatcher import pool_selector

    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    task_id = "stderr-pool"
    path = store.record_path(task_id).parent / "logs" / f"{task_id}.stdout.log"
    path.parent.mkdir(parents=True)
    stdout = json.dumps({"type": "turn.failed", "error": {"message": signal}}) if structured else ""
    path.write_text(stdout)
    parsed = codex.parse(stdout, signal, 1, None)
    assert pool_selector._provider_exhausted({"task_id": task_id, "agent": "codex", "error": parsed.error}) is structured


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_agy_review_binds_findings_schema(dispatch):
    """Defect class: review dispatch silently omits its structured verdict schema."""
    spec, record = dispatch.invoke(path=None, agent="agy", review=True)
    assert record["require_review_verdict"] is True
    assert record["output_schema_path"].endswith("/schemas/code-review-findings.v1.schema.json")
    argv = agy.build(spec, {}).argv
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["schema_version"]["const"] == "code-review-findings.v1"
    assert schema["required"] == ["schema_version", "overall", "findings"]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_agy_ukrainian_review_omits_findings_schema(dispatch):
    """Defect class: Ukrainian review is forced to bind the code-review findings schema."""
    spec, record = dispatch.invoke(path=None, agent="agy", review=True, review_profile="ukrainian")
    assert record["require_review_verdict"] is True
    assert record.get("output_schema_path") is None
    assert record.get("output_schema_sha256") is None
    argv = agy.build(spec, {}).argv
    assert "--json-schema" not in argv


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("case", ["valid", "missing-block", "malformed-json", "wrong-schema"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_agy_ukrainian_review_preserves_item_judgments_and_refuses_malformed(case):
    """Defect class: Ukrainian item judgments are dropped by schema enforcement, or invalid blocks are accepted."""
    from lu_machinery.review.review_contract import ContractError
    from lu_machinery.review.review_contract import build_item_packet
    from lu_machinery.review.review_contract import judge_item_reply

    packet = build_item_packet([{"id": "item-1", "text": "слово"}])
    if case == "valid":
        reply = (
            "VERDICT: APPROVE\n\n"
            "```ukrainian-item-judgments\n"
            '{"schema": "ukrainian-item-judgments.v1", "items": [{"id": "item-1", "verdict": "APPROVE", "finding": "", "sources": "VESUM", "quote": "слово"}]}\n'
            "```\n"
        )
    elif case == "missing-block":
        reply = "VERDICT: APPROVE\n\nNo item judgments block provided."
    elif case == "malformed-json":
        reply = "VERDICT: APPROVE\n\n```ukrainian-item-judgments\n{not valid json\n```\n"
    elif case == "wrong-schema":
        reply = (
            "VERDICT: APPROVE\n\n"
            "```ukrainian-item-judgments\n"
            '{"schema": "code-review-findings.v1", "items": [{"id": "item-1", "verdict": "APPROVE"}]}\n'
            "```\n"
        )

    stream = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": reply}}) + "\n"
    parsed = agy.parse(stream, "", 0, reply, output_schema=None)
    assert parsed.error is None
    assert parsed.text == reply

    if case == "valid":
        chunk = judge_item_reply(packet, parsed.text, stated="APPROVED", ukrainian=True)
        assert chunk.coverage == ("item-1",)
        assert chunk.void is None
    elif case == "missing-block":
        with pytest.raises(ContractError, match="ITEM_JUDGMENTS_MISSING"):
            judge_item_reply(packet, parsed.text, stated="APPROVED", ukrainian=True)
    else:
        with pytest.raises(ContractError, match="ITEM_JUDGMENTS_MALFORMED"):
            judge_item_reply(packet, parsed.text, stated="APPROVED", ukrainian=True)


def _item_review_record(tmp_path, profile="ukrainian"):
    import hashlib

    from lu_machinery.review.review_contract import build_item_packet
    from lu_machinery.review.review_contract import render_item_packet

    brief = tmp_path / "brief.prompt"
    brief.write_text("Review.\n\n" + render_item_packet(build_item_packet([{"id": "item-1", "text": "слово"}])))
    return {"schema_version": 2, "require_review_verdict": True, "review_profile": profile,
            "prompt_file": str(brief), "prompt_sha256": hashlib.sha256(brief.read_bytes()).hexdigest()}


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_verdict_gate_accepts_ukrainian_item_judgments_block(tmp_path):
    """Defect class (#12417): a contract-only Ukrainian item-judgments reply fails the verdict gate."""
    reply = (
        "```ukrainian-item-judgments\n"
        '{"schema": "ukrainian-item-judgments.v1", "items": [{"id": "item-1", "verdict": "APPROVE", '
        '"finding": "", "sources": "VESUM", "quote": "слово"}]}\n'
        "```\n"
    )
    assert worker._verdict_failure(_item_review_record(tmp_path), reply) is None


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_verdict_gate_refuses_ukrainian_reply_without_block_or_verdict(tmp_path):
    """Defect class (#12417): a Ukrainian reply with neither a valid block nor a verdict passes the gate."""
    reply = "```ukrainian-item-judgments\n{not valid json\n```\n"
    assert worker._verdict_failure(_item_review_record(tmp_path), reply) == "review_missing_verdict_line"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_verdict_gate_code_review_without_verdict_still_fails(tmp_path):
    """Defect class (#12417): the item-judgments path leaks into code-review profiles."""
    reply = (
        "```ukrainian-item-judgments\n"
        '{"schema": "ukrainian-item-judgments.v1", "items": [{"id": "item-1", "verdict": "APPROVE", '
        '"finding": "", "sources": "VESUM", "quote": "слово"}]}\n'
        "```\n"
    )
    assert worker._verdict_failure(_item_review_record(tmp_path, "code"), reply) == "review_missing_verdict_line"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("case", ["valid", "conflicting-response", "missing", "null", "invalid",
                                 "terminal-error", "duplicate-terminal", "exit-failure"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_agy_structured_terminal_not_response(case):
    """Defect class: response prose or an unsuccessful terminal substitutes for structured findings."""
    from lu_machinery.review.review_contract import load_schema
    from lu_machinery.review.verdict_parser import recognized_verdicts

    payload = {"schema_version": "code-review-findings.v1", "findings": [],
               "overall": {"correctness": "correct", "explanation": "No defect found.", "confidence": 0.9}}
    terminal = {"status": "SUCCESS", "structured_output": payload, "response": json.dumps(payload)}
    if case == "conflicting-response":
        terminal["response"] = "VERDICT: REQUEST_CHANGES"
    elif case == "missing":
        terminal.pop("structured_output")
    elif case == "null":
        terminal["structured_output"] = None
    elif case == "invalid":
        terminal["structured_output"] = {"verdict": "APPROVE"}
    elif case == "terminal-error":
        terminal["status"] = "ERROR"
    stream = json.dumps({"event": "result", "result": terminal}) + "\n"
    if case == "duplicate-terminal":
        stream += stream
    parsed = agy.parse(stream, "", int(case == "exit-failure"), json.dumps(payload), output_schema=load_schema())
    if case in {"valid", "conflicting-response"}:
        assert parsed.error is None
        assert json.loads(parsed.text) == payload
        assert recognized_verdicts(parsed.text) == ["APPROVE"]
        assert worker._verdict_failure({"schema_version": 2, "require_review_verdict": True}, parsed.text) is None
    else:
        assert parsed.error is not None


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_agy_schema_argument_too_large_refused(dispatch, capsys):
    """Defect class: an oversized inline schema reaches process spawn rather than a typed refusal."""
    dispatch.schema.write_text(json.dumps({"type": "object", "description": "x" * (128 * 1024)}))
    dispatch.invoke(agent="agy", expected=2)
    assert "agy_output_schema_unsupported" in capsys.readouterr().err


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_agy_structured_missing_capability_preserved():
    """Defect class: native schema erases an existing capability-failure declaration."""
    from lu_machinery.review.review_contract import load_schema
    from lu_machinery.review.verdict_parser import recognized_verdicts

    payload = {"schema_version": "code-review-findings.v1", "findings": [],
               "overall": {"correctness": "uncertain", "explanation": "Required capability unavailable.",
                           "confidence": 1}}
    schema = load_schema()
    for capability in ("sources-mcp", "test-execution", "shell", "browser", "checkout-data"):
        payload["missing_capabilities"] = [capability]
        stream = json.dumps({"event": "result", "result": {"status": "SUCCESS", "structured_output": payload}})
        parsed = agy.parse(stream, "", 0, None, output_schema=schema)
        assert parsed.error is None
        assert json.loads(parsed.text)["missing_capabilities"] == [capability]
        assert recognized_verdicts(parsed.text) == ["BLOCKED"]
    for declaration in ([], ["shell", "shell"], ["unknown"], "shell", None, [1]):
        payload["missing_capabilities"] = declaration
        stream = json.dumps({"event": "result", "result": {"status": "SUCCESS", "structured_output": payload}})
        parsed = agy.parse(stream, "", 0, None, output_schema=schema)
        assert parsed.error is not None
        assert "structured_output_invalid" in parsed.error
        assert not parsed.text
    payload["missing_capabilities"] = ["shell"]
    payload["overall"]["correctness"] = "correct"
    stream = json.dumps({"event": "result", "result": {"status": "SUCCESS", "structured_output": payload}})
    parsed = agy.parse(stream, "", 0, None, output_schema=schema)
    assert parsed.error is not None
    assert "additionalProperties" in parsed.error
    assert not parsed.text


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("output_schema_hooks")
def test_review_path_pattern_portable_equivalence():
    """Defect class: a portable-schema rewrite changes which repository paths are accepted."""
    from lu_machinery.review.review_contract import load_schema

    original = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))"
                          r"[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$")
    portable = re.compile(load_schema()["$defs"]["repo_relative_path"]["pattern"])
    cases = [
        ("a", True), (".", True), ("./a", True), ("a/.", True), ("...", True),
        ("..hidden", True), ("a/..hidden", True), (".agent/file", True),
        ("scripts/dispatcher/agy.py", True), ("+a/@b_1-c.txt", True),
        ("", False), ("..", False), ("../a", False), ("a/..", False),
        ("a/../b", False), ("a//b", False), ("/a", False), ("a/", False),
        ("C:/a", False), ("a\\b", False), ("a:b", False), ("~/.ssh", False),
        ("a b", False), ("a\nb", False), ("./..", False),
    ]
    for path, expected in cases:
        assert (original.search(path) is not None, portable.search(path) is not None) == (expected, expected), path


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("phase", ["before-init", "after-init", "success"])
@pytest.mark.usefixtures("output_schema_hooks")
def test_agy_schema_rejection_is_typed_before_model_init(phase):
    """Defect class: a native schema refusal loses its reason or is misclassified after initialization."""
    from lu_machinery.review.review_contract import load_schema

    message = ("invalid --json-schema: standard validation failed\n\n"
               "- at schema: invalid regular expression\n    - unsupported Perl syntax: `(?!`")
    stderr = f"Error: {message}\n\nUsage of agy:\n  --json-schema Optional schema\n"
    payload = {"schema_version": "code-review-findings.v1", "findings": [],
               "overall": {"correctness": "correct", "explanation": "No defect found.", "confidence": 0.9}}
    stdout = "" if phase == "before-init" else json.dumps(
        {"event": "init", "init": {"model": "gemini-3.8-flash-high"}}
    ) + "\n"
    if phase == "success":
        stdout += json.dumps({"event": "result", "result": {"status": "SUCCESS", "structured_output": payload}})
    parsed = agy.parse(stdout, stderr, 0 if phase == "success" else 1, None, output_schema=load_schema())
    if phase == "before-init":
        assert parsed.error == (
            "agy_output_schema_unsupported: AGY rejected the --json-schema argument; "
            "its text is in the task's retained stream"
        )
        assert parsed.text is None and parsed.resolved_model is None
    elif phase == "after-init":
        assert parsed.error == "agy: exited with status 1"
    else:
        assert parsed.error is None and json.loads(parsed.text) == payload




@pytest.fixture
def output_schema_hooks(agent_runtime_roots, monkeypatch):
    from lu_machinery.review.product_hooks import attempt_hooks
    monkeypatch.setattr(attempt_hooks(), "openai_structured_output_forbidden_keywords", lambda: frozenset({"allOf", "oneOf", "not", "if", "then", "else", "dependentRequired", "dependentSchemas", "propertyNames", "patternProperties", "minProperties", "maxProperties", "uniqueItems"}), raising=False)

pytestmark = [pytest.mark.usefixtures('configured_roots')]
