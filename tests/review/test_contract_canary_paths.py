"""Contract gates recognize changes in both repository layouts."""



from lu_machinery.review.contract_canary import CONTRACT_PATHS, touches_contract
import hashlib
import json
import subprocess
from pathlib import Path
import pytest
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.review import contract_canary as canary
from tests.helpers.product_tree import configured_roots as configured_roots

def test_contract_paths_match_product_layout() -> None:
    """Prevent installed keepers from missing product review-contract changes."""
    package_paths = [path for path in CONTRACT_PATHS if path.startswith("lu_machinery/")]
    assert package_paths
    for path in package_paths:
        assert touches_contract([path.replace("lu_machinery/", "scripts/", 1)])
    assert touches_contract(["scripts/review/record_cf_verdict.py"])
    assert touches_contract(["schemas/code-review-findings.v1.schema.json"])
    assert not touches_contract(["scripts/unrelated.py"])


CONTRACT_FILE = "scripts/review/review_contract.py"


RECEIPT = json.dumps({
    "schema_version": "code-review-findings.v1",
    "overall": {"correctness": "correct", "explanation": "No defects.", "confidence": 0.9},
    "findings": [],
})


MODELS = {
    "agy": "gemini-3.8-flash-high",
    "claude": "claude-sonnet-5-5",
    "codex": "gpt-6.1-sol",
    "cursor": "grok-4.7-high",
    "muse": "muse-wonder-full-1.0",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=sanitized_git_env(), timeout=10
    ).stdout.strip()


TASKS: dict[str, tuple[dict, str]] = {}


REAL_LOAD_TASK = canary._load_task


@pytest.fixture()
def tasks(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[dict, str]]:
    """The settled task records receipts name, in place of the dispatcher store."""
    TASKS.clear()

    def load(task_id: str) -> tuple[dict, str]:
        if task_id not in TASKS:
            raise ValueError("task not found")
        return TASKS[task_id]

    monkeypatch.setattr(canary, "_load_task", load)
    return TASKS


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "scripts/review").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "trunk")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / CONTRACT_FILE).write_text("CONTRACT = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root, _git(root, "rev-parse", "HEAD")


def task(route: str, head: str, **changes: object) -> dict:
    record = {
        "task_id": f"contract-canary-1-{route}",
        "status": "done",
        "mode": "read-only",
        "require_review_verdict": True,
        "schema_version": 2,
        "review_profile": "code",
        "head_sha": head,
        "worktree_start_sha": head,
        "review_branch": None,
        "pr": None,
        "agent": route,
        "model": MODELS[route],
        "dispatcher_code": {"root": "/candidate", "head": head, "clean": True, "read_only": True},
        "dispatcher_code_start": {"root": "/candidate", "head": head, "clean": True, "read_only": True},
        "result_sha256": hashlib.sha256(RECEIPT.encode()).hexdigest(),
    }
    # The providers that attest the model from their own stream, as the live records show.
    if route == "cursor":
        record.update(resolved_model="Grok 4.7 256K High", resolved_model_known=True,
                      resolved_model_source="cursor-stream-json")
    elif route == "agy":
        record.update(resolved_model=MODELS["agy"], resolved_model_known=True, resolved_model_source="agy-stream-json")
    record.update(changes)
    return record


def write_all(root: Path, head: str) -> None:
    for route in canary.required_routes():
        record = task(route, head)
        TASKS[record["task_id"]] = (record, RECEIPT)
        receipt = canary.build_receipt(record, RECEIPT, head=head)
        path = canary.receipt_root(root) / head / f"{route}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt), encoding="utf-8")


@pytest.mark.usefixtures('configured_roots', 'tasks')
@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"head_sha": "f" * 40}, "did not review the PR head"),
        ({"worktree_start_sha": "f" * 40}, "did not review the PR head"),
        ({"review_branch": "claude/x"}, "branch- or PR-pinned"),
        ({"pr": 1}, "branch- or PR-pinned"),
        ({"status": "failed"}, "did not settle done"),
        ({"review_profile": "ukrainian"}, "did not settle through the receipt gate"),
        ({"review_attempt": "/manifest.json"}, "did not settle through the receipt gate"),
        ({"dispatcher_code": {"head": "f" * 40, "clean": True}}, "candidate's clean dispatcher"),
        ({"dispatcher_code": None}, "candidate's clean dispatcher"),
        ({"agent": "kimi"}, "not a required reviewer route"),
        ({"model": "claude-fable-5-1"}, "not a formal reviewer"),
        ({"language_lane": True}, "did not settle through the receipt gate"),
        ({"dispatcher_code_start": {"root": "/other", "head": "f" * 40, "clean": True}}, "candidate's clean dispatcher"),
        ({"dispatcher_code_start": None}, "candidate's clean dispatcher"),
        ({"result_sha256": "0" * 64}, "does not match"),
    ],
)
def test_record_refuses_a_task_that_is_not_canary_evidence(repo, changes: dict, why: str) -> None:
    _, head = repo
    with pytest.raises(canary.CanaryError, match=why):
        canary.build_receipt(task("claude", head, **changes), RECEIPT, head=head)


@pytest.mark.usefixtures('configured_roots', 'tasks')
@pytest.mark.parametrize(
    ("route", "changes"),
    [
        ("cursor", {"resolved_model": "Claude Fable 5.1"}),
        ("cursor", {"resolved_model_known": False}),
        ("agy", {"resolved_model": "claude-fable-5-1"}),
        ("agy", {"resolved_model_known": False}),
    ],
)
def test_record_refuses_a_route_whose_attested_model_is_not_formal(repo, route: str, changes: dict) -> None:
    _, head = repo
    canary.build_receipt(task(route, head), RECEIPT, head=head)  # the attested formal model passes
    with pytest.raises(canary.CanaryError, match="did not attest a formal reviewer"):
        canary.build_receipt(task(route, head, **changes), RECEIPT, head=head)


@pytest.mark.usefixtures('configured_roots', 'tasks')
def test_a_verdict_line_is_not_a_canonical_receipt(repo) -> None:
    _, head = repo
    text = "VERDICT: APPROVE\n"
    record = task("claude", head, result_sha256=hashlib.sha256(text.encode()).hexdigest())
    with pytest.raises(canary.CanaryError, match="canonical receipt"):
        canary.build_receipt(record, text, head=head)


@pytest.mark.usefixtures('configured_roots', 'tasks')
def test_receipts_bound_to_another_route_or_head_refuse(repo) -> None:
    root, head = repo
    write_all(root, head)
    path = canary.receipt_root(root) / head / "agy.json"
    receipt = json.loads(path.read_text())
    path.write_text(json.dumps({**receipt, "dispatcher_head": "0" * 40}))
    assert canary.refusal([CONTRACT_FILE], head, root) == "unverifiable: receipt for agy does not match its task"
    path.write_text("[" * 100_000 + "]" * 100_000)  # too deeply nested to decode: refuse, never crash
    assert canary.refusal([CONTRACT_FILE], head, root) == "unverifiable: receipt for agy"
    path.write_text(json.dumps({**receipt, "route": "codex"}))
    assert canary.refusal([CONTRACT_FILE], head, root) == "unverifiable: receipt for agy"
    path.write_text(json.dumps({**receipt, "eligible_as_review": True}))
    assert canary.refusal([CONTRACT_FILE], head, root) == "unverifiable: receipt for agy"
    # The same receipts do not carry to a new head.
    (root / CONTRACT_FILE).write_text("CONTRACT = 2\n", encoding="utf-8")
    _git(root, "commit", "-qam", "change")
    assert canary.refusal([CONTRACT_FILE], _git(root, "rev-parse", "HEAD"), root).startswith("missing: ")


@pytest.mark.usefixtures('configured_roots', 'tasks')
def test_a_receipt_must_still_match_its_settled_task(repo, tasks) -> None:
    root, head = repo
    write_all(root, head)
    record, _ = tasks.pop("contract-canary-1-codex")
    assert canary.refusal([CONTRACT_FILE], head, root) == "unverifiable: task behind the receipt for codex"
    tasks["contract-canary-1-codex"] = ({**record, "status": "failed"}, RECEIPT)
    assert canary.refusal([CONTRACT_FILE], head, root) == "unverifiable: task behind the receipt for codex"
    tasks["contract-canary-1-codex"] = (record, RECEIPT)
    path = canary.receipt_root(root) / head / "codex.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "model": "gpt-6-astra"}))
    assert canary.refusal([CONTRACT_FILE], head, root) == "unverifiable: receipt for codex does not match its task"


@pytest.mark.usefixtures('configured_roots', 'tasks')
def test_a_reply_with_carriage_returns_keeps_its_byte_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The dispatcher digests the result file's bytes; reading it must not translate newlines.
    from lu_machinery.dispatcher import store

    reply = RECEIPT.replace(",", ",\r\n")
    result = tmp_path / "t.result"
    result.write_bytes(reply.encode())
    head = "c" * 40
    record = task("claude", head, result_file=str(result), result_sha256=hashlib.sha256(result.read_bytes()).hexdigest())
    monkeypatch.setattr(store, "read", lambda task_id: record)
    _, text = REAL_LOAD_TASK("t")
    assert canary.build_receipt(record, text, head=head)["result_sha256"] == record["result_sha256"]


@pytest.mark.usefixtures('configured_roots', 'tasks')
def test_the_gate_trusts_the_candidate_settlement_not_mains_contract(repo, tasks) -> None:
    # A breaking contract change: main's parser would refuse the reply the candidate settled done.
    root, head = repo
    write_all(root, head)
    reply = json.dumps({"schema_version": "code-review-findings.v2", "verdict": "approve"})
    record, _ = tasks["contract-canary-1-codex"]
    tasks["contract-canary-1-codex"] = ({**record, "result_sha256": hashlib.sha256(reply.encode()).hexdigest()}, reply)
    path = canary.receipt_root(root) / head / "codex.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "result_sha256": tasks["contract-canary-1-codex"][0]["result_sha256"]}))
    assert canary.refusal([CONTRACT_FILE], head, root) is None
    with pytest.raises(canary.CanaryError, match="canonical receipt"):
        canary.build_receipt(tasks["contract-canary-1-codex"][0], reply, head=head)


@pytest.mark.usefixtures('configured_roots', 'tasks')
def test_the_gate_itself_is_a_contract_path() -> None:
    assert canary.touches_contract(["scripts/review/contract_canary.py"])


@pytest.mark.usefixtures('configured_roots', 'tasks')
def test_a_change_outside_the_contract_needs_no_canary(repo) -> None:
    root, _ = repo
    # No git read at all: an unknown head does not matter outside the contract.
    assert canary.refusal(["scripts/example.py", "docs/review-protocol.md"], "not-a-sha", root) is None


@pytest.fixture
def runner_roots(tmp_path, configured_roots, monkeypatch):
    """A host context with all external orchestration replaced by an argv fake."""
    import sys
    from lu_machinery.dispatcher import git as dispatch_git

    source = tmp_path / "registered-source"
    source.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(dispatch_git, "resolve_repo", lambda key: (
        source, {"github": "example/machinery", "key": key},
    ))
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    monkeypatch.delenv("LEARN_UKRAINIAN_DISPATCH_TASK_ID", raising=False)
    return home


def test_runner_dry_run_never_mounts_dispatches_or_writes(runner_roots, monkeypatch, capsys):
    """A worker can inspect the complete plan without escaping its write boundary."""
    monkeypatch.setenv("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "worker-585")
    monkeypatch.setattr(canary, "_invoke", lambda *a, **kw: pytest.fail("dry-run executed a command"))
    assert canary.main(["run", "--head", "a" * 40, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "no live proof" in output
    assert "mksquashfs" in output and "snapfuse" in output and "fusermount" in output
    assert "--codex-account b-sub" in output and "--claude-account b" in output
    assert "--codex-account a" not in output
    assert "--require-hashes" in output and "contract-canary.pth" in output
    assert "--pinned-head " + "a" * 40 in output
    assert "check --head " + "a" * 40 in output
    assert output.count("systemd-run --user") == 4
    assert list(runner_roots.iterdir()) == []
    assert canary.main(["run", "--head", "a" * 40]) == 2
    assert "outside a dispatched worker" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [
    None, "route", "check", "mount", "unmount", "unmount-noop", "interrupt", "unit-lost",
])
def test_runner_failures_cannot_score_green_or_delete_a_live_mount(
    runner_roots, monkeypatch, capsys, failure,
):
    """Route/check failure fails closed; setup and cleanup errors preserve a mounted tree."""
    commands = []
    mounted = False

    def invoke(argv, *, cwd, env, check=True):
        nonlocal mounted
        commands.append(argv)
        assert "PYTHONPATH" not in env
        code, output = 0, ""
        if argv[0] == "mksquashfs":
            Path(argv[2]).write_bytes(b"candidate image")
        if argv[0] == "snapfuse":
            mounted = True
            if failure == "mount":
                raise canary.CanaryError("mount failed after becoming visible")
        if argv[0] == "systemd-run":
            assert mounted
            route = argv[argv.index("--agent") + 1]
            assert argv[argv.index("--mode") + 1] == "read-only"
            assert "--pr" not in argv and "--branch" not in argv
            assert argv[argv.index("--task-id") + 1][0].isalnum()
            assert any(a.startswith("--setenv=PATH=") for a in argv)
            assert any(a.startswith("--setenv=LEARN_UKRAINIAN_PRIMARY_REPO_ROOT=") for a in argv)
            assert any(a.startswith("--setenv=LU_PRODUCT_ROOT=") for a in argv)
            if failure == "interrupt":
                raise KeyboardInterrupt
            code = 1 if failure == "route" and route == "claude" else 0
            output = "" if failure == "unit-lost" else "CANARY-ROUTE-SETTLED\n"
        if "check" in argv:
            code = 1 if failure == "check" else 0
            output = "needs-contract-canary: missing: codex\n" if code else "ok\n"
        if "-u" in argv:
            if failure == "unmount":
                raise canary.CanaryError("unmount busy")
            mounted = failure == "unmount-noop"
        return subprocess.CompletedProcess(argv, code, output, "")

    monkeypatch.setenv("PYTHONPATH", "/must/not/shadow/candidate")
    monkeypatch.setattr(canary, "_invoke", invoke)
    monkeypatch.setattr(canary.os.path, "ismount", lambda path: mounted)
    expected = 0 if failure is None else 1 if failure in {"route", "check"} else 2
    assert canary.main(["run", "--head", "b" * 40]) == expected
    output = capsys.readouterr()
    if failure in {None, "route", "check"}:
        assert sum(cmd[0] == "systemd-run" for cmd in commands) == 4
        assert "squashfs sha256:" in output.out
        assert "claude: FAIL" in output.out if failure == "route" else "claude: PASS" in output.out
    if failure in {"unmount", "unmount-noop", "interrupt", "unit-lost"}:
        assert mounted
        assert "retained" in output.err
        assert len(list(runner_roots.iterdir())) == 1
    else:
        assert not mounted
        assert list(runner_roots.iterdir()) == []


@pytest.mark.parametrize("bad_args", [
    ["--head", "main"], ["--head", "c" * 40, "--hard-timeout", "0"],
])
def test_runner_rejects_unpinned_or_unbounded_runs(runner_roots, monkeypatch, bad_args):
    """No task or image may be created for a ref or an unlimited provider run."""
    monkeypatch.setattr(canary, "_invoke", lambda *a, **kw: pytest.fail("invalid run executed"))
    assert canary.main(["run", *bad_args]) == 2
    assert list(runner_roots.iterdir()) == []


def test_runner_resolves_pr_head_once_before_planning(runner_roots, monkeypatch, capsys):
    """A moving PR never mixes the dispatch and score commits."""
    from lu_machinery.common import github_client

    reads = []

    def read(argv, **kwargs):
        reads.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"headRefOid": "d" * 40}), "")

    monkeypatch.setattr(github_client, "run", read)
    assert canary.main(["run", "--repo", "machinery", "--pr", "42", "--dry-run"]) == 0
    assert len(reads) == 1
    assert "--repo" in reads[0] and "example/machinery" in reads[0]
    assert "--pinned-head " + "d" * 40 in capsys.readouterr().out


@pytest.mark.parametrize("outcome", ["done", "failed", "interrupted"])
def test_route_bootstrap_records_only_terminal_success_and_cancels_interruption(monkeypatch, outcome):
    """A failed or interrupted task cannot be promoted to a canonical canary receipt."""
    import signal
    import sys
    from lu_machinery.dispatcher import cli

    calls = []
    receipts = []

    def main(argv):
        calls.append(argv[0])
        if argv[0] == "dispatch":
            print("canary-task\nnonce-42")
            return 0
        assert argv[1:] == ["canary-task", "--run-nonce", "nonce-42"]
        if argv[0] == "wait" and calls.count("wait") == 1:
            if outcome == "interrupted":
                raise KeyboardInterrupt
            return 1 if outcome == "failed" else 0
        return 0

    monkeypatch.setattr(cli, "main", main)
    monkeypatch.setattr(canary, "record", lambda task_id: receipts.append(task_id))
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["bootstrap", "dispatch"])
    if outcome == "done":
        exec(compile(canary._RUN_ROUTE, "<canary-bootstrap>", "exec"), {})
        assert calls == ["dispatch", "wait"]
        assert receipts == ["canary-task"]
    elif outcome == "failed":
        with pytest.raises(SystemExit) as exc:
            exec(compile(canary._RUN_ROUTE, "<canary-bootstrap>", "exec"), {})
        assert exc.value.code == 1
        assert calls == ["dispatch", "wait"]
        assert receipts == []
    else:
        with pytest.raises(KeyboardInterrupt):
            exec(compile(canary._RUN_ROUTE, "<canary-bootstrap>", "exec"), {})
        assert calls == ["dispatch", "wait", "cancel", "wait"]
        assert receipts == []
