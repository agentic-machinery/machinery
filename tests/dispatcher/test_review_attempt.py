"""Bound review attempts: admission, single-use identity and execution inside the attempt boundary.

Restored from learn-ukrainian ``tests/dispatcher/test_review_attempt.py`` at 5aba2adb (machinery #904).
The product removed its copy with its in-tree dispatcher; the behaviour ships here, in
``lu_machinery.dispatcher`` (``cli``, ``store``, ``review_attempt``) and
``lu_machinery.agent_runtime.attempt_boundary``. Assertions on the product recorder
(``scripts.review.record``) and the product prompt renderer stay with the product.

The receipt ledger is a product hook (``attempt_hooks().ledger()``). The Ukrainian claim cases
plant a test-owned JSONL ledger with the hook's interface (``records``, ``LedgerError``), so
they exercise machinery's claim validation and nothing of the product's ledger.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lu_machinery.dispatcher import checks, cli, git, store, worker
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.review.render_contract import trusted_core_context
from tests.helpers import current_sol
from tests.helpers.product_tree import configured_roots  # noqa: F401

CURRENT_SOL = current_sol()

pytestmark = pytest.mark.usefixtures("configured_roots")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """Every Git call here (fixture setup and the dispatcher's own) reads no caller, global or system config.

    Autouse, so it applies before ``configured_roots`` initializes its repository. It drops every inherited
    ``GIT_*`` variable (``GIT_CONFIG_COUNT`` and its indexed keys and values, ``GIT_CONFIG_PARAMETERS``,
    ``GIT_DIR`` and the rest) and points the global config at a test-owned file that also disables the default
    ignore and attributes files Git would otherwise read from the caller's home.
    """
    import os

    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    config = tmp_path_factory.mktemp("git-config") / "gitconfig"
    # Unset, these two fall back to files under $XDG_CONFIG_HOME or $HOME (git/ignore, git/attributes).
    config.write_text(f"[core]\n\texcludesFile = {os.devnull}\n\tattributesFile = {os.devnull}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("inputs: []\n")
    return ["--review-id", "review", "--attempt-id", "attempt", "--review-attempt", str(manifest)]


def invoke(attempt, *flags):
    return cli.main(["dispatch", "--agent", "sol", "--model", CURRENT_SOL, "--prompt", "Review", *attempt, *flags])


@pytest.mark.parametrize("flags", [("--mode", "workspace-write"), ("--cwd", "existing")])
def test_attempt_cannot_write_or_reuse_a_worktree(attempt, monkeypatch, capsys, flags):
    """Defect class: a bound attempt runs with write access or inside a borrowed worktree."""
    launch = Mock()
    monkeypatch.setattr(worker, "start", launch)
    assert invoke(attempt, *flags) == 2
    assert "review attempt requires" in capsys.readouterr().err
    launch.assert_not_called()


def test_dry_run_reports_binding_without_spending_attempt(attempt, tmp_path, monkeypatch, capsys):
    """Defect class: a dry run spends the attempt id or plans an unbound review."""
    monkeypatch.setattr(git, "resolve_repo", lambda *_: (tmp_path, dict(key="public", github="example/public")))
    monkeypatch.setattr(git, "worktree_plan", lambda *a, **k: (tmp_path, "codex/review", "a" * 40))
    monkeypatch.setattr(checks, "check_resources", lambda _: None)
    # The synthetic head has no Git history to read branch authorship from.
    monkeypatch.setattr(checks, "check_branch_author_families", lambda *a: None)
    for _ in range(2):
        assert invoke(attempt, "--dry-run") == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["review_attempt"] == dict(review_id="review", attempt_id="attempt",
                                             manifest_sha256=store.digest(b"inputs: []\n"))
        assert plan["review_access"] == "isolated"
    assert not (tmp_path / "tasks").exists()
    assert not (tmp_path / "review-receipts").exists()


def test_bound_attempt_cannot_spend_an_exhausted_branch_pr_budget(attempt, tmp_path, monkeypatch, capsys):
    """Defect class: a bound attempt is charged to a branch's ordinary PR review budget (#12216)."""
    from lu_machinery.dispatcher import review_attempt
    from lu_machinery.review import record_cf_verdict as recorder

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    for index in range(3):
        (tasks / f"prior-{index}.json").write_text(json.dumps(dict(
            task_id=f"prior-{index}", schema_version=2, repository="example/public", status="failed",
            mode="read-only", review_profile="infra", review_branch="review/target",
            worktree_base_sha=str(index + 1) * 40,
        )))
    before = {path.name: path.read_bytes() for path in tasks.iterdir()}
    monkeypatch.setattr(recorder, "_open_pr_number", lambda *_: None)
    budget = Mock(wraps=checks.check_review_budget)
    monkeypatch.setattr(checks, "check_review_budget", budget)
    monkeypatch.setattr(git, "resolve_repo", lambda *_: (tmp_path, dict(key="public", github="example/public")))
    monkeypatch.setattr(git, "review_branch_start", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(git, "worktree_plan", lambda *a, **k: (tmp_path / "checkout", "codex/review", "a" * 40))
    monkeypatch.setattr(git, "create_worktree", Mock())
    monkeypatch.setattr(checks, "check_branch_author_families", lambda *a: None)
    monkeypatch.setattr(checks, "check_resources", lambda _: None)
    monkeypatch.setattr(worker, "settle_left_worktrees", lambda: None)
    monkeypatch.setattr(review_attempt, "admit", lambda *a: {})
    launch = Mock()
    monkeypatch.setattr(worker, "start", launch)

    for selector in (("--branch", "review/target"), ("--pr", "42")):
        assert invoke(attempt, *selector, "--review-profile", "infra") == 2
        error = capsys.readouterr().err
        assert "review_attempt_pr_selector_unsupported" in error
        assert "docs/runbooks/formal-review-attempt-isolation.md#running-a-bound-attempt" in error
        assert "--pinned-head" in error
        budget.assert_not_called()
        launch.assert_not_called()
        git.create_worktree.assert_not_called()
        assert {path.name: path.read_bytes() for path in tasks.iterdir()} == before

    assert invoke([], "--branch", "review/target", "--review-profile", "infra") == 2
    assert "REVIEW_BUDGET_EXHAUSTED" in capsys.readouterr().err
    budget.assert_called_once()
    launch.assert_not_called()
    git.create_worktree.assert_not_called()
    # Since machinery #892 the refused budget dispatch leaves one typed never-started record; earlier records stay.
    after = {path.name: path.read_bytes() for path in tasks.iterdir()}
    assert {name: after[name] for name in before} == before
    [refusal] = [json.loads(after[name]) for name in after.keys() - before.keys() if name.endswith(".json")]
    assert after.keys() - before.keys() == {f"{refusal['task_id']}.json", f"{refusal['task_id']}.json.lock"}
    assert (refusal["status"], refusal["never_started_reason"], refusal["pid"]) == ("failed", "review_setup_refusal", None)

    budget.reset_mock()
    assert invoke(attempt, "--pinned-head", "a" * 40, "--review-profile", "infra") == 0
    budget.assert_not_called()
    launch.assert_called_once()


@pytest.mark.parametrize("suffix", [".jsonl", ".jsonl.sha256", ".mcp.json", ".codex-home", ".agy-home"])
def test_legacy_artifact_spends_attempt_even_without_task_record(attempt, tmp_path, suffix, capsys):
    """Defect class: a receipt artifact left without a task record lets the attempt id run twice."""
    receipts = tmp_path / "review-receipts" / "review"
    receipts.mkdir(parents=True)
    (receipts / f"attempt{suffix}").symlink_to("missing")
    assert invoke(attempt, "--dry-run") == 2
    assert "already spent" in capsys.readouterr().err


@pytest.mark.parametrize("schema", [1, 2])
def test_archived_record_spends_attempt_across_manifest_changes(attempt, tmp_path, schema, capsys):
    """Defect class: an archived attempt runs again once its manifest (and so its hash) changes.

    The pair is spent by review and attempt id alone; the archived binding names another manifest hash.
    """
    archive = tmp_path / "tasks" / "archive"
    archive.mkdir(parents=True)
    (archive / "prior.json").write_text(json.dumps(dict(task_id="prior", schema_version=schema, status="failed", review_attempt={
        "review_id": "review", "attempt_id": "attempt", "manifest_sha256": "b" * 64})))
    assert invoke(attempt, "--dry-run") == 2
    assert "already spent" in capsys.readouterr().err


@pytest.mark.parametrize("access", [None, "isolated", "full"])
def test_binding_snapshot_attests_v2_return_and_survives_record_corruption(attempt, tmp_path, access):
    """Defect class: a later manifest edit changes an admitted binding, or a corrupt record lets the pair run again."""
    if access:
        attempt += ["--review-access", access]
    args = cli.build_parser().parse_args(["dispatch", "--agent", "sol", "--model", CURRENT_SOL, *attempt])
    binding = store.review_binding(vars(args))
    store.check_review_attempt(binding, claim=True)
    # Admission's snapshot must survive a subsequent manifest edit.
    (tmp_path / "manifest.yaml").write_text("changed\n")
    prompt = cli._PromptPath(tmp_path / "tasks" / "task.prompt")
    prompt.content = b"Review\n"
    spec = TaskSpec("task", "codex", CURRENT_SOL, None, "read-only", tmp_path, prompt, prompt.with_suffix(".result"))
    record = store.new_record(spec, dict(key="public", github="example/public"), "codex/task", "a" * 40,
                              dict(vars(args), research_owned_path=[]), binding=binding)
    store.create(record, b"Review\n")
    record = store.read("task")
    assert record["schema_version"] == 2 and record["review_attempt"] == binding
    assert binding == dict(review_id="review", attempt_id="attempt", manifest_sha256=store.digest(b"inputs: []\n"))
    assert record["prompt_sha256"] == store.digest(b"Review\n") and record["review_access"] == (access or "isolated")
    store.record_path("task").write_text("{")
    with pytest.raises(ValueError, match="unreadable task record"):
        store.check_review_attempt(binding, claim=True)
    assert (tmp_path / "review-receipts/review/attempt.jsonl").is_file()


@pytest.mark.parametrize("flags", [["--review-access", "isolated"], ["--attempt-id", "attempt"],
                                  ["--review-id", "../review", "--attempt-id", "attempt", "--review-attempt", "a" * 64]])
def test_partial_or_unsafe_binding_refused_without_launch(flags, capsys):
    """Defect class: a partial binding launches unbound, or a path-like id escapes the receipts directory."""
    assert invoke(flags) == 2
    expected = "must be used together" if len(flags) == 2 else "invalid review"
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize("manifest", ["a" * 64, "."])
def test_manifest_must_be_a_file(attempt, manifest, capsys):
    """Defect class: a hash or directory stands in for the manifest the attempt is bound to."""
    attempt[-1] = manifest
    assert invoke(attempt, "--dry-run") == 2
    assert "must name a manifest file" in capsys.readouterr().err


def test_strict_listing_skips_evidence_append_beside_task_records(tmp_path, monkeypatch):
    """Defect class: a sidecar beside a task record is listed as a task, or shadows the running record."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    records = [{"task_id": task_id, "status": "done", "result_file": None} for task_id in ("first", "second")]
    for record in records:
        (tmp_path / f"{record['task_id']}.json").write_text(json.dumps(record))
    (tmp_path / "first.evidence-append.json").write_text("[]")

    assert store.list_tasks(strict=True) == records

    # A sandbox receipt must not shadow its running task in ownership lookups.
    records[0]["status"] = "running"
    (tmp_path / "first.json").write_text(json.dumps(records[0]))
    (tmp_path / "first.sandbox.json").write_text(json.dumps({"schema": 1, "task_id": "first", "status": "ran"}))
    listed = store.list_tasks(strict=True)
    assert listed == records
    assert {record["task_id"]: record for record in listed}["first"]["status"] == "running"


def test_concurrent_admissions_claim_one_pair_for_different_tasks(attempt, tmp_path, monkeypatch):
    """Defect class: two admissions that both pass the record scan both claim the same pair."""
    from lu_machinery.agent_runtime import review_mcp

    binding = dict(review_id="review", attempt_id="attempt", manifest_sha256="a" * 64)
    barrier, create = Barrier(2), review_mcp._create_file

    def exclusive(*args):
        barrier.wait(timeout=10)  # Force both admissions past the record/artifact scan.
        create(*args)

    monkeypatch.setattr(review_mcp, "_create_file", exclusive)

    def admit(task_id):
        try:
            store.check_review_attempt(binding, claim=True)
        except ValueError:
            return "spent"
        store.create(dict(task_id=task_id, prompt_file=str(tmp_path / f"{task_id}.prompt"),
                          review_attempt=binding), b"Review")
        return "admitted"

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(admit, ["first", "second"])) == ["admitted", "spent"]
    assert len(store.list_tasks()) == 1


def test_lexical_render_admission_rejects_tampering(tmp_path, monkeypatch):
    """Defect class: lexical admission refuses valid renders or accepts altered prompts, identity or schema.

    Restored with the real machinery contract check (``review_mcp.check_review_contract``) on a render record
    built by machinery's own ``render_record``. Only the product hooks (the prompt checker, pins and receipt
    tools) and the Sources server location are planted, at their boundaries.
    """
    import hashlib
    import subprocess

    import yaml

    from lu_machinery.agent_runtime import attempt_boundary, review_mcp
    from lu_machinery.dispatcher import review_attempt
    from lu_machinery.review import product_hooks
    from lu_machinery.review.render_contract import RENDER_RECORD_KEY, render_record, render_record_path

    # The render checkout: a Sources server, its lock and the lexical template.
    checkout = (tmp_path / "render").resolve()
    (checkout / ".mcp/servers/sources").mkdir(parents=True)
    (checkout / ".mcp/servers/sources/server.py").write_text("import json\n")
    (checkout / "requirements-lock.txt").write_text("fixture==1.0\n")
    prompts = checkout / "scripts/review/prompts"
    prompts.mkdir(parents=True)
    (prompts / "lexical-review.md.j2").write_text("Review {{ items }}\n")
    monkeypatch.setattr(review_mcp, "review_server_checkout", lambda: checkout)

    # Git configuration is isolated by the module's autouse ``isolated_git`` fixture.
    # The dispatch commit: one public candidate file the manifest pins.
    inputs = (tmp_path / "inputs").resolve()
    inputs.mkdir()
    candidate = b"candidate words\n"
    (inputs / "candidates.txt").write_bytes(candidate)
    for command in (["git", "init", "-q"], ["git", "add", "candidates.txt"],
                    ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "Public candidate fixture"]):
        subprocess.run(command, cwd=inputs, check=True, capture_output=True, timeout=30)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=inputs, timeout=30).decode().strip()
    pin = {"path": "candidates.txt", "sha256": hashlib.sha256(candidate).hexdigest()}
    manifest = {"kind": "lexical", "inputs": {"candidates": pin}}
    manifest_path = tmp_path / "lexical.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))

    tools = frozenset({"verify_words"})
    ledger = SimpleNamespace(REVIEW_TOOLS=tools, review_tools=lambda access="isolated", server_path=None: tools)
    hooks = SimpleNamespace(
        check_prompt=lambda *a, **k: SimpleNamespace(passed=True, errors=[]),
        pinned_entries=lambda loaded: [("inputs.candidates", loaded["inputs"]["candidates"])],
        pin_refusals=lambda *a: [], ledger=lambda: ledger,
    )
    for module in (product_hooks, review_mcp):  # review_mcp binds the hook at import
        monkeypatch.setattr(module, "attempt_hooks", lambda **_: hooks)

    prefix, dependency = trusted_core_context()
    prompt = prefix + "Rendered lexical review\nattempt: review/attempt\n"
    prompt_file = tmp_path / "lexical.prompt"
    prompt_file.write_text(prompt, encoding="utf-8")
    template = {"lexical-review.md.j2": hashlib.sha256((prompts / "lexical-review.md.j2").read_bytes()).hexdigest()}
    record = render_record(checkout, prompts, template, hashlib.sha256(prompt.encode()).hexdigest(),
                           review_id="review", attempt_id="attempt", input_root=inputs,
                           machinery_dependencies={"rules_core": dependency})
    render_record_path(prompt_file).write_text(json.dumps({RENDER_RECORD_KEY: record, "files_read": []}))
    args = cli.build_parser().parse_args([
        "dispatch", "--agent", "agy", "--model", "gemini-4-neon-high", "--effort", "high",
        "--mode", "read-only", "--review-profile", "ukrainian", "--review-author-model", CURRENT_SOL,
        "--review-id", "review", "--attempt-id", "attempt", "--review-attempt", str(manifest_path),
        "--prompt-file", str(prompt_file), "--review-access", "isolated",
    ])
    binding = dict(review_id="review", attempt_id="attempt")

    contract = review_attempt.admit(args, prompt.encode(), binding, inputs, sha)
    assert contract["templates"] == ["lexical-review.md.j2"]
    assert contract["prompt_sha256"] == record["prompt_sha256"]
    assert attempt_boundary.authorized_closure(manifest, inputs) == {"candidates.txt": candidate}
    with pytest.raises(ValueError, match="review_render_record_stale"):
        review_attempt.admit(args, (prompt + "altered").encode(), binding, inputs, sha)
    # The file and the supplied bytes agree; only the render record names other bytes.
    prompt_file.write_text(prompt + "altered", encoding="utf-8")
    with pytest.raises(ValueError, match="review_render_record_stale"):
        review_attempt.admit(args, (prompt + "altered").encode(), binding, inputs, sha)
    prompt_file.write_text(prompt, encoding="utf-8")
    with pytest.raises(ValueError, match="review_render_record_attempt_mismatch"):
        review_attempt.admit(args, prompt.encode(), dict(binding, attempt_id="other"), inputs, sha)
    for name, invalid in (("review_profile", "code"), ("review_author_model", None), ("effort", "low"),
                          ("output_schema", "override.json")):
        original = getattr(args, name)
        setattr(args, name, invalid)
        reason = "attempt_output_schema_unsupported" if name == "output_schema" else "lexical_review_identity_required"
        with pytest.raises(ValueError, match=reason):
            review_attempt.admit(args, prompt.encode(), binding, inputs, sha)
        setattr(args, name, original)


def _ledger_hook(monkeypatch):
    """Plant a JSONL receipt ledger behind the product hook ``attempt_hooks().ledger()``."""
    from lu_machinery.review import product_hooks

    class LedgerError(Exception):
        pass

    def records(path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    ledger = SimpleNamespace(records=records, LedgerError=LedgerError)
    monkeypatch.setattr(product_hooks, "attempt_hooks", lambda **_: SimpleNamespace(ledger=lambda: ledger))


def _jsonl(records) -> bytes:
    return b"".join(json.dumps(record, ensure_ascii=False).encode() + b"\n" for record in records)


def _run_manifest_ukrainian_attempt(
    attempt, tmp_path, monkeypatch, reply: str, *, ledger_records=(), foreign_records=()
):
    from lu_machinery.agent_runtime import attempt_boundary, review_mcp, runner
    from lu_machinery.dispatcher import headroom

    _ledger_hook(monkeypatch)
    args = cli.build_parser().parse_args([
        "dispatch", "--agent", "agy", "--model", "gemini-3.8-flash-high", *attempt, "--review-profile", "ukrainian",
    ])
    binding = store.review_binding(vars(args))
    prefix, dependency = trusted_core_context()
    rendered = (prefix + "Rendered review prompt\nattempt: review/attempt\n").encode()
    prompt = cli._PromptPath(tmp_path / "tasks" / "task.prompt")
    prompt.content = rendered
    workspace = tmp_path / "checkout"
    workspace.mkdir(exist_ok=True)
    spec = TaskSpec("task", "agy", "gemini-3.8-flash-high", "high", "read-only", workspace, prompt,
                    tmp_path / "tasks" / "task.result", options=dict(review_attempt=str(tmp_path / "manifest.yaml")))
    record = store.new_record(spec, dict(key="public", github="example/public"), "agy/task", "a" * 40,
                              dict(vars(args), research_owned_path=[]), binding=binding)
    record["review_contract"] = dict(input_root=str(tmp_path), machinery_dependencies={"rules_core": dependency},
                                     prompt_sha256=store.digest(rendered))
    path = store.create(record, rendered)

    for name in ("check_plan", "check_runtime"):
        monkeypatch.setattr(headroom, name, lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_reap_settled", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "settle_left_worktrees", lambda *_a, **_k: None)
    monkeypatch.setattr(git, "snapshot_delivery", lambda *_a: {})
    monkeypatch.setattr(attempt_boundary, "verify_full_review_tree", lambda *a: None)
    receipts = tmp_path / "review-receipts" / "review"
    receipts.mkdir(parents=True, exist_ok=True)
    ledger_file = receipts / "attempt.jsonl"

    if foreign_records:
        foreign_receipts = tmp_path / "review-receipts" / "foreign_review"
        foreign_receipts.mkdir(parents=True, exist_ok=True)
        foreign_lines = _jsonl(foreign_records)
        (foreign_receipts / "attempt.jsonl").write_bytes(foreign_lines)
        (foreign_receipts / "attempt.jsonl.sha256").write_text(store.digest(foreign_lines) + "\n")

    def prepare(review_id, attempt_id, manifest, harness, **kwargs):
        config = receipts / "attempt.mcp.json"
        config.write_text("{}")
        ledger_lines = _jsonl(ledger_records)
        ledger_file.write_bytes(ledger_lines)
        (receipts / "attempt.jsonl.sha256").write_text(store.digest(ledger_lines) + "\n")
        return review_mcp.ReviewMcpPlan(config, {}, ledger_file, receipts / "attempt.jsonl.sha256",
                                        binding["manifest_sha256"], review_id, attempt_id, harness)

    monkeypatch.setattr(review_mcp, "prepare_review_attempt", prepare)
    monkeypatch.setattr(review_mcp, "review_attempt_tool_config", lambda *a, **k: {})

    def invoke_runtime(agent, text, **kwargs):
        return SimpleNamespace(ok=True, response=reply, identity_state="observed",
                               observed_model="gemini-3.8-flash-high", observed_model_source="provider",
                               failure_code=None, stderr_excerpt=None)

    monkeypatch.setattr(runner, "invoke", invoke_runtime)
    worker.supervise(path, {}, 60)
    done = store.read("task")
    return done, spec.final_file.read_text(), binding


def _receipt(**changes):
    return {
        "receipt_id": "rr-supported1111111111", "review_id": "review", "attempt_id": "attempt",
        "manifest_sha256": store.digest(b"inputs: []\n"), "tool": "verify_words",
        "arguments": {"words": ["паляниця"]}, "result": "паляниця: attested in VESUM as noun", **changes,
    }


def _judgments(*items) -> str:
    return ("VERDICT: APPROVE\n```ukrainian-item-judgments\n"
            + json.dumps({"schema": "ukrainian-item-judgments.v1", "items": list(items)}) + "\n```\n")


def _yaml_reply(payload) -> str:
    import yaml

    return "VERDICT: APPROVE\n```yaml\n" + yaml.safe_dump(payload) + "```\n"


def test_manifest_bound_ukrainian_review_supported_claim_accepted(attempt, tmp_path, monkeypatch):
    """Defect class: a quote found in this attempt's own receipt is dropped. It must be accepted and counted."""
    reply = _judgments({"id": "card-1", "verdict": "APPROVE", "finding": "Attested word", "sources": "VESUM",
                        "evidence": [{"receipt": "rr-supported1111111111", "quote": "attested in VESUM"}]})
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[_receipt()])
    assert done["status"] == "done", done["error"]
    assert done["claims_total"] == 2
    assert done["claims_unsupported"] == 0
    assert done["unknown_items"] == 0
    assert done["dropped_claims"] == []
    assert "rr-supported1111111111" in final_text
    assert "_approval_void" not in final_text


def test_manifest_bound_ukrainian_review_fabricated_quote_dropped_and_counted(attempt, tmp_path, monkeypatch):
    """Defect class: a quote that the cited receipt's result does not contain survives into the saved review."""
    reply = _judgments({"id": "card-1", "verdict": "APPROVE", "finding": "Attested word", "sources": "VESUM",
                        "evidence": [{"receipt": "rr-supported1111111111", "quote": "fabricated quote never returned"}]})
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[_receipt()])
    assert done["claims_total"] == 2
    assert done["claims_unsupported"] == 2
    assert done["unknown_items"] == 0
    assert len(done["dropped_claims"]) == 2
    assert done["dropped_claims"][1]["claim"] == {"sources": "VESUM"}
    assert done["dropped_claims"][1]["reason"] == "claim_not_in_captured_output"
    assert done["dropped_claims"][1]["captured_event_ids"] == []
    assert done["dropped_claims"][0]["reason"] == "quote_not_in_result"
    assert "fabricated quote never returned" not in final_text
    assert "_approval_void" in final_text


def test_manifest_bound_ukrainian_review_foreign_attempt_receipt_dropped(attempt, tmp_path, monkeypatch):
    """Defect class: a receipt from another review attempt supports a claim in this one."""
    foreign = _receipt(receipt_id="rr-foreign99999999999", review_id="other_review", attempt_id="other_attempt",
                       manifest_sha256="f" * 64, result="some foreign result")
    reply = _judgments({"id": "card-1", "verdict": "APPROVE", "finding": "Foreign citation", "sources": "VESUM",
                        "evidence": [{"receipt": "rr-foreign99999999999", "quote": "some foreign result"}]})
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          foreign_records=[foreign])
    assert done["claims_total"] == 2
    assert done["claims_unsupported"] == 2
    assert len(done["dropped_claims"]) == 2
    assert done["dropped_claims"][0]["reason"] == "foreign_attempt_receipt"
    assert "rr-foreign99999999999" not in final_text


def test_manifest_bound_ukrainian_review_unknown_item_leaves_denominator_incomplete(attempt, tmp_path, monkeypatch):
    """Defect class: an item judged unknown is counted as covered."""
    from lu_machinery.review.review_contract import build_item_packet, judge_item_reply

    reply = _judgments({"id": "card-1", "verdict": "APPROVE", "finding": "Known good", "sources": "VESUM"},
                       {"id": "card-2", "verdict": "unknown", "finding": "Inconclusive evidence", "sources": "none"})
    known = _receipt(receipt_id="rr-known", arguments={"words": ["тест"]}, result="VESUM: тест")
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[known])
    assert done["unknown_items"] == 1
    assert done["claims_total"] == 1
    assert done["claims_unsupported"] == 0

    packet = build_item_packet([{"id": "card-1", "text": "тест"}, {"id": "card-2", "text": "приклад"}])
    chunk = judge_item_reply(packet, final_text, stated="APPROVED", ukrainian=True)
    assert chunk.denominator == ("card-1", "card-2")
    assert chunk.coverage == ("card-1",)
    assert "card-2" not in chunk.coverage


def test_manifest_bound_ukrainian_review_yaml_fabricated_quote_dropped_and_counted(attempt, tmp_path, monkeypatch):
    """Defect class: a YAML review return skips the quote check that the item-judgment form gets."""
    reply = _yaml_reply({"review_schema": 1, "kind": "settle", "evidence": [
        {"receipt": "rr-supported1111111111", "quote": "fabricated quote not in result"}]})
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[_receipt()])
    assert done["claims_total"] == 1
    assert done["claims_unsupported"] == 1
    assert len(done["dropped_claims"]) == 1
    assert done["dropped_claims"][0]["reason"] == "quote_not_in_result"
    assert "fabricated quote not in result" not in final_text


def test_manifest_bound_ukrainian_review_two_items_one_void_preserves_valid_item(attempt, tmp_path, monkeypatch):
    """Defect class: one unsupported item voids the whole reply, or its unsupported claim survives."""
    from lu_machinery.review.review_contract import build_item_packet, judge_item_reply

    reply = _judgments(
        {"id": "card-1", "verdict": "APPROVE", "finding": "Attested word", "sources": "VESUM", "quote": "паляниця",
         "evidence": [{"receipt": "rr-supported1111111111", "quote": "attested in VESUM"}]},
        {"id": "card-2", "verdict": "APPROVE", "finding": "Fabricated quote", "sources": "VESUM", "quote": "хліб",
         "evidence": [{"receipt": "rr-supported1111111111", "quote": "fake quote"}]},
    )
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[_receipt()])
    assert done["claims_total"] == 4
    assert done["claims_unsupported"] == 2
    assert len(done["dropped_claims"]) == 2
    assert done["dropped_claims"][1]["claim"] == {"sources": "VESUM"}
    assert done["dropped_claims"][1]["reason"] == "claim_not_in_captured_output"
    assert done["dropped_claims"][1]["captured_event_ids"] == []
    assert done["dropped_claims"][0]["item_id"] == "card-2"
    assert done["dropped_claims"][0]["reason"] == "quote_not_in_result"

    packet = build_item_packet([{"id": "card-1", "text": "паляниця"}, {"id": "card-2", "text": "хліб"}])
    chunk = judge_item_reply(packet, final_text, stated="APPROVED", ukrainian=True)
    assert chunk.denominator == ("card-1", "card-2")
    assert chunk.coverage == ("card-1",)
    assert "card-2" not in chunk.coverage
    assert chunk.void is None


def test_manifest_bound_ukrainian_review_yaml_finding_fabricated_expected_dropped_and_counted(
    attempt, tmp_path, monkeypatch
):
    """Defect class: a valid receipt launders a fabricated ``expected`` text in the same finding."""
    reply = _yaml_reply({"review_schema": 1, "kind": "lesson", "findings": [
        {"id": "F-1", "claim": "Spelling issue", "expected": "fabricated expected text",
         "evidence": {"receipt": "rr-supported1111111111"}}]})
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[_receipt()])
    assert done["claims_total"] == 2
    assert done["claims_unsupported"] == 1
    assert len(done["dropped_claims"]) == 1
    assert done["dropped_claims"][0]["item_id"] == "F-1"
    assert done["dropped_claims"][0]["reason"] == "quote_not_in_result"
    assert "fabricated expected text" not in final_text
    assert "rr-supported1111111111" in final_text  # the receipt itself was valid and stays


def test_manifest_bound_ukrainian_review_yaml_finding_valid_expected_retained(attempt, tmp_path, monkeypatch):
    """Defect class: a finding whose ``expected`` text its receipt supports is dropped."""
    reply = _yaml_reply({"review_schema": 1, "kind": "lesson", "findings": [
        {"id": "F-1", "claim": "Spelling issue", "expected": "attested in VESUM",
         "evidence": {"receipt": "rr-supported1111111111"}}]})
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[_receipt()])
    assert done["claims_total"] == 2
    assert done["claims_unsupported"] == 0
    assert done["dropped_claims"] == []
    assert "attested in VESUM" in final_text
    assert "rr-supported1111111111" in final_text


def test_manifest_bound_ukrainian_review_per_item_receipt_only_rejected_as_malformed_claim(
    attempt, tmp_path, monkeypatch
):
    """Defect class: a receipt cited without a quote counts as support."""
    from lu_machinery.review.review_contract import build_item_packet, judge_item_reply

    reply = _judgments({"id": "card-1", "verdict": "APPROVE", "finding": "Attested word", "sources": "VESUM",
                        "quote": "паляниця", "evidence": [{"receipt": "rr-supported1111111111"}]})
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[_receipt()])
    assert done["claims_total"] == 2
    assert done["claims_unsupported"] == 2
    assert len(done["dropped_claims"]) == 2
    assert done["dropped_claims"][1]["claim"] == {"sources": "VESUM"}
    assert done["dropped_claims"][1]["reason"] == "claim_not_in_captured_output"
    assert done["dropped_claims"][1]["captured_event_ids"] == []
    assert done["dropped_claims"][0]["item_id"] == "card-1"
    assert done["dropped_claims"][0]["reason"] == "malformed_claim"

    packet = build_item_packet([{"id": "card-1", "text": "паляниця"}])
    chunk = judge_item_reply(packet, final_text, stated="APPROVED", ukrainian=True)
    assert chunk.denominator == ("card-1",)
    assert chunk.coverage == ()


@pytest.mark.parametrize("claim,output", [
    ("Example Reference page 7", "Example Reference page 2"),
    ("Rule 8: soft-group", "Rule 8: hard-group"),
    ("VESUM tag noun:inanim:f:v_naz", "VESUM tag noun:anim:f:v_naz"),
])
def test_ukrainian_claim_text_cannot_hide_behind_valid_receipt_quote(attempt, tmp_path, monkeypatch, claim, output):
    """Defect class: a valid receipt quote launders an assertion that the same output contradicts."""
    rec = _receipt(receipt_id="rr-same-attempt", tool="verify_word", arguments={"word": "example"}, result=output)
    unrelated = {**rec, "receipt_id": "rr-other-event", "arguments": {"word": "other"}, "result": claim}
    reply = _judgments({"id": "card-1", "verdict": "APPROVE", "finding": "Appropriate activity.",
                        "sources": claim, "evidence": [{"receipt": rec["receipt_id"], "quote": output}]})
    done, final_text, _ = _run_manifest_ukrainian_attempt(attempt, tmp_path, monkeypatch, reply,
                                                          ledger_records=[rec, unrelated])
    assert done["claims_total"] == 2
    assert done["claims_unsupported"] == 1
    assert done["dropped_claims"][0]["claim"] == {"sources": claim}
    assert done["dropped_claims"][0]["reason"] == "claim_not_in_captured_output"
    assert done["dropped_claims"][0]["captured_event_ids"] == ["rr-same-attempt"]
    row = json.loads(final_text.split("```ukrainian-item-judgments\n")[1].split("\n```")[0])["items"][0]
    assert row["_approval_void"] is True
    assert row["evidence"] == [{"receipt": rec["receipt_id"], "quote": output}]


# --- Codex account b in bound attempts (#12504, C-4635) -------------------------------------------------------------


@pytest.fixture
def codex_accounts_ab(tmp_path, monkeypatch):
    """Account a's default home and account b's bound home, each holding its own distinct login."""
    import os

    a = tmp_path / "home" / ".codex"
    a.mkdir(parents=True)
    b = tmp_path / "codex-b"
    b.mkdir(mode=0o700)
    for home, body in ((a, b'{"login": "account-a"}'), (b, b'{"login": "account-b"}')):
        (home / "auth.json").write_bytes(body)
        (home / "auth.json").chmod(0o600)
    binding = tmp_path / "codex_accounts.json"
    binding.write_text(json.dumps({"schema_version": 1, "accounts": {"b-sub": {"codex_home": str(b), "budget_usd": 200}}}))
    binding.chmod(0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text("#!/bin/sh\nexit 0\n")  # `codex login status` reports a login
    (bin_dir / "codex").chmod(0o755)
    monkeypatch.setenv("HOME", str(a.parent))
    monkeypatch.setenv("LU_CODEX_ACCOUNTS_FILE", str(binding))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for name in ("CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        monkeypatch.setenv(name, "unset")  # recorded, so teardown also undoes the supervisor's own CODEX_HOME
        monkeypatch.delenv(name)
    return a, b


@pytest.mark.parametrize("label,admitted", [
    (None, True), ("sub", True), ("b-sub", True), ("api", False), ("b-api", False),
])
def test_bound_attempt_admits_only_subscription_codex_accounts(label, admitted):
    """Defect class: a bound attempt refuses account b, or runs on an API-billed route with no spending cap."""
    from lu_machinery.dispatcher import review_attempt

    args = SimpleNamespace(review_access=None, full_checkout=False, output_schema=None, codex_account=label,
                           prompt_file=None)
    expected = "review_attempt_prompt_file_required" if admitted else "review_attempt_account_unsupported"
    with pytest.raises(ValueError, match=expected):
        review_attempt.admit(args, b"", {}, None, "a" * 40)


def test_unknown_codex_account_refuses_a_bound_attempt_before_it_is_spent(attempt, tmp_path, monkeypatch, capsys):
    """Defect class: an unknown account label is substituted by a known one, or spends the attempt id."""
    launch = Mock()
    monkeypatch.setattr(worker, "start", launch)
    assert invoke(attempt, "--codex-account", "c-sub") == 2
    assert "codex_account_unknown" in capsys.readouterr().err
    launch.assert_not_called()
    assert not (tmp_path / "review-receipts").exists()
    # Since machinery #892 the refusal leaves a typed record; it must not carry the binding that spends the pair.
    records = [json.loads(path.read_text()) for path in (tmp_path / "tasks").glob("*.json")]
    assert [(record["never_started_reason"], "review_attempt" in record) for record in records] == [
        ("account_check", False)]
    args = cli.build_parser().parse_args(["dispatch", "--agent", "sol", "--model", CURRENT_SOL, *attempt])
    store.check_review_attempt(store.review_binding(vars(args)))  # still unspent


@pytest.mark.parametrize("label", ["c-sub", "API", ""])
def test_attempt_boundary_refuses_an_unknown_codex_account_without_staging(codex_accounts_ab, tmp_path, label):
    """Defect class: an unknown label stages some account's login instead of refusing."""
    from lu_machinery.agent_runtime.attempt_boundary import stage_codex_account_auth
    from lu_machinery.review.isolation import ReviewIsolationError

    with pytest.raises(ReviewIsolationError, match="attempt_codex_account_unknown"):
        stage_codex_account_auth(label, tmp_path / "staged")
    assert not (tmp_path / "staged").exists()


def test_attempt_boundary_stages_each_codex_account_own_login(codex_accounts_ab, tmp_path):
    """Defect class: a b-sub attempt authenticates with account a's login, or the default account changes source."""
    from lu_machinery.agent_runtime.attempt_boundary import stage_codex_account_auth

    a, b = codex_accounts_ab
    for label, own in ((None, a), ("sub", a), ("b-sub", b), ("b", b)):
        staged = tmp_path / f"staged-{label}"
        env = stage_codex_account_auth(label, staged)
        assert (staged / ".codex" / "auth.json").read_bytes() == (own / "auth.json").read_bytes()
        assert env["CODEX_HOME"] == str(staged / ".codex")


@pytest.mark.parametrize("case,code", [
    ("ambient_home", "attempt_codex_account_ambient_conflict"),
    ("ambient_key", "attempt_codex_account_ambient_conflict"),
    ("unbound", "attempt_codex_account_binding_missing"),
    ("logged_out", "attempt_codex_account_not_logged_in"),
    ("keyring_login", "attempt_codex_account_login_missing"),
])
def test_attempt_boundary_never_substitutes_account_a_for_b(codex_accounts_ab, tmp_path, monkeypatch, case, code):
    """Defect class: when account b's login cannot be staged, account a's (or an ambient credential) is used."""
    from lu_machinery.agent_runtime.attempt_boundary import stage_codex_account_auth
    from lu_machinery.review.isolation import ReviewIsolationError

    a, b = codex_accounts_ab
    if case == "ambient_home":
        monkeypatch.setenv("CODEX_HOME", str(a))
    elif case == "ambient_key":
        monkeypatch.setenv("OPENAI_API_KEY", "fixture-credential")
    elif case == "unbound":
        monkeypatch.setenv("LU_CODEX_ACCOUNTS_FILE", str(tmp_path / "absent.json"))
    elif case == "logged_out":
        codex = tmp_path / "bin" / "codex"
        codex.write_text("#!/bin/sh\nexit 1\n")
    else:
        (b / "auth.json").unlink()  # `codex login status` passes, but the login is not a stageable file
    staged = tmp_path / "staged"
    with pytest.raises(ReviewIsolationError) as refused:
        stage_codex_account_auth("b-sub", staged)
    assert str(refused.value) == code
    login = staged / ".codex" / "auth.json"
    assert not login.exists() or login.read_bytes() != (a / "auth.json").read_bytes()


def test_bound_attempt_on_codex_b_runs_as_account_b(codex_accounts_ab, attempt, tmp_path, monkeypatch):
    """A b-sub attempt routes its label into the boundary, and the supervisor runs, stages and is metered as b.

    Defect class: a bound attempt on b stages account a's login, or its usage and rate limits count against a.
    """
    import os

    from lu_machinery.agent_runtime import attempt_boundary, review_mcp, runner, usage
    from lu_machinery.dispatcher import headroom

    a, b = codex_accounts_ab
    _ledger_hook(monkeypatch)
    args = cli.build_parser().parse_args(["dispatch", "--agent", "sol", "--model", CURRENT_SOL, *attempt,
                                          "--codex-account", "b-sub", "--review-access", "full"])
    binding = store.review_binding(vars(args))
    prefix, dependency = trusted_core_context()
    rendered = (prefix + "Rendered review prompt\nattempt: review/attempt\n").encode()
    prompt = cli._PromptPath(tmp_path / "tasks" / "task.prompt")
    prompt.content = rendered
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    spec = TaskSpec("task", "codex", CURRENT_SOL, "high", "read-only", workspace, prompt,
                    tmp_path / "tasks" / "task.result", options=dict(review_attempt=str(tmp_path / "manifest.yaml")),
                    codex_account="b-sub")
    record = store.new_record(spec, dict(key="public", github="example/public"), "codex/task", "a" * 40,
                              dict(vars(args), research_owned_path=[]), binding=binding)
    assert record["codex_account"] == "b-sub"
    record["review_contract"] = dict(input_root=str(tmp_path), machinery_dependencies={"rules_core": dependency},
                                     prompt_sha256=store.digest(rendered))
    path = store.create(record, rendered)
    for name in ("check_plan", "check_runtime"):
        monkeypatch.setattr(headroom, name, lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_reap_settled", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "settle_left_worktrees", lambda *_a, **_k: None)
    monkeypatch.setattr(git, "snapshot_delivery", lambda *_a: {})
    monkeypatch.setattr(attempt_boundary, "verify_full_review_tree", lambda *a: None)
    receipts = tmp_path / "review-receipts" / "review"

    def prepare(review_id, attempt_id, manifest, harness, **kwargs):
        config = receipts / "attempt.mcp.json"
        config.write_text("{}")
        (receipts / "attempt.jsonl.sha256").write_text(store.digest(b"") + "\n")
        return review_mcp.ReviewMcpPlan(config, {}, receipts / "attempt.jsonl", receipts / "attempt.jsonl.sha256",
                                        binding["manifest_sha256"], review_id, attempt_id, harness)

    monkeypatch.setattr(review_mcp, "prepare_review_attempt", prepare)
    seen = []

    def invoke_runtime(agent, text, **kwargs):
        label = kwargs["tool_config"][attempt_boundary.CODEX_ACCOUNT_KEY]
        staged = tmp_path / "boundary-home"
        attempt_boundary.stage_codex_account_auth(label, staged)  # what AttemptBoundary stages for this label
        seen.append((label, os.environ["CODEX_HOME"], usage.current_codex_account(),
                     (staged / ".codex" / "auth.json").read_bytes()))
        return SimpleNamespace(ok=True, response="review: done\n", identity_state="unknown", observed_model=None,
                               observed_model_source=None, failure_code=None, stderr_excerpt=None)

    monkeypatch.setattr(runner, "invoke", invoke_runtime)
    worker.supervise(path, {}, 60)

    assert store.read("task")["error"] is None, store.read("task")["error"]
    assert seen == [("b-sub", str(b), "b-sub", (b / "auth.json").read_bytes())]
    assert seen[0][3] != (a / "auth.json").read_bytes()
    assert store.read("task")["status"] == "done"


@pytest.mark.parametrize("suffix", [" ", " ", "　"])
def test_attempt_boundary_refuses_a_bound_home_that_staging_would_rename(codex_accounts_ab, tmp_path, monkeypatch, suffix):
    """F1 (#12732 round 1): a bound home named like account a's home plus trailing whitespace stays b's.

    Defect class: staging normalizes the bound CODEX_HOME (``str.strip``) into another directory and copies
    account a's login for b.
    """
    from lu_machinery.agent_runtime.attempt_boundary import stage_codex_account_auth
    from lu_machinery.review.isolation import ReviewIsolationError

    a, b = codex_accounts_ab
    twin = a.parent / f".codex{suffix}"
    twin.mkdir(mode=0o700)
    (twin / "auth.json").write_bytes((b / "auth.json").read_bytes())
    (twin / "auth.json").chmod(0o600)
    binding = tmp_path / "codex_accounts.json"
    binding.write_text(json.dumps({"schema_version": 1, "accounts": {"b-sub": {"codex_home": str(twin), "budget_usd": 200}}}))
    staged = tmp_path / "staged"
    with pytest.raises(ReviewIsolationError, match=r"^attempt_codex_account_home_not_canonical$"):
        stage_codex_account_auth("b-sub", staged)
    login = staged / ".codex" / "auth.json"
    assert not login.exists() or login.read_bytes() != (a / "auth.json").read_bytes()
