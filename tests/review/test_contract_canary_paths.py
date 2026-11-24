"""Contract gates recognize changes to the machinery package's review contract files."""



from lu_machinery.review.contract_canary import CONTRACT_PATHS, touches_contract
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.review import contract_canary as canary
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.helpers import current_sol

CURRENT_SOL = current_sol()

GIB = 2**30

def test_contract_paths_match_product_layout() -> None:
    """Use the root runtime lock and the machinery modules, excluding retired product copies."""
    package_paths = [path for path in CONTRACT_PATHS if path.startswith("lu_machinery/")]
    assert set(CONTRACT_PATHS) - set(package_paths) == {"uv.lock"}
    for path in package_paths:
        assert touches_contract([path])
        assert not touches_contract([path.replace("lu_machinery/", "scripts/", 1)])
    assert not touches_contract(["schemas/code-review-findings.v1.schema.json"])
    assert not touches_contract(["lu_machinery/unrelated.py"])
    assert touches_contract(["uv.lock"])
    assert not touches_contract([".dagger/uv.lock"])


CONTRACT_FILE = "lu_machinery/review/review_contract.py"


RECEIPT = json.dumps({
    "schema_version": "code-review-findings.v1",
    "overall": {"correctness": "correct", "explanation": "No defects.", "confidence": 0.9},
    "findings": [],
})


MODELS = {
    "agy": "gemini-4-neon-high",
    "claude": "claude-sonnet-5-5",
    "codex": CURRENT_SOL,
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
    (root / "lu_machinery/review").mkdir(parents=True)
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
        ("agy", {"resolved_model": "gemini-4-hydrogen-medium"}),
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
    assert canary.touches_contract(["lu_machinery/review/contract_canary.py"])


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
    # The space check reads the default refuse level and a roomy disk, never the host's.
    monkeypatch.setenv("DISPATCH_ADMISSION_ENV_FILE", "")
    monkeypatch.delenv("DISPATCH_DISK_REFUSE_USED_PERCENT", raising=False)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(used=GIB, free=1000 * GIB))
    (tmp_path / "wheelhouse").mkdir()
    monkeypatch.setenv(canary.WHEELHOUSE_ENV, str(tmp_path / "wheelhouse"))
    # Every metered account has fresh allowance, never the host Monitor's reading.
    from lu_machinery.fleet import credit_lane

    monkeypatch.setattr(credit_lane, "read_routing_budget", lambda **kw: funded_snapshot(a=80.0, b=80.0))
    return home


def funded_snapshot(*, a: float, b: float) -> dict:
    """A fresh routing budget with Codex and Claude accounts a and b at the given remaining %."""
    probe = {"status": "healthy", "freshness": "fresh", "age_s": 60.0}
    return {
        "diagnostics": {"stale": False},
        "agents": {"codex": {**probe, "remaining_pct": a}, "claude": {**probe, "remaining_pct": a}},
        "codex_accounts": {"codex:b": {**probe, "remaining_pct": b}},
        "claude_accounts": {"claude:b": {"status": "ok", "stale": False, "age_s": 60.0,
                                         "weekly": {"left_pct": b}, "session5h": {"left_pct": b}}},
    }


def fake_setup(argv, cwd) -> None:
    """The files the setup commands leave that the runner reads or removes."""
    if argv[:2] == ["git", "init"]:
        Path(argv[-1]).mkdir()
    if argv[0] == "mksquashfs":
        Path(argv[2]).write_bytes(b"candidate image")
    if argv[0] == "snapfuse":
        # Only the image may remain: the checkout is gone before anything runs from the mount.
        assert not (cwd / "source").exists()


def test_runner_dry_run_never_mounts_dispatches_or_writes(runner_roots, monkeypatch, capsys):
    """Defect class: scoring candidate routes with an installed gate that rejects new models."""
    import sys

    monkeypatch.setenv("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "worker-585")
    monkeypatch.setattr(canary, "_invoke", lambda *a, **kw: pytest.fail("dry-run executed a command"))
    assert canary.main(["run", "--head", "a" * 40, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "no live proof" in output
    assert "mksquashfs" in output and "snapfuse" in output and "fusermount" in output
    # Only the candidate commit is fetched, and its checkout goes before the mount is used.
    assert "git clone" not in output and "fetch --quiet --no-tags --depth 1 " in output
    assert output.index("mksquashfs") < output.index("/source (the image holds") < output.index("snapfuse")
    assert "dispatch admission's disk refuse level" in output
    assert "--no-emit-project --no-default-groups --all-extras --no-extra test" in output
    # lu-machinery is on no package index: the host wheelhouse supplies it, the lock's hashes pick the bytes.
    assert f"--require-hashes --only-binary=:all: --find-links {runner_roots.parent / 'wheelhouse'} -r " in output
    # With allowance on both accounts, account b drains first (the reviewer resolver's order).
    assert "--codex-account b-sub" in output and "--claude-account b " in output
    assert "--codex-account sub " not in output and "--claude-account a " not in output
    assert "--require-hashes" in output and "contract-canary.pth" in output
    assert "--pinned-head " + "a" * 40 in output
    assert "check --head " + "a" * 40 in output
    candidate = runner_roots / ".lu-contract-canary-DRY-RUN/consumer/bin/python"
    assert f"{candidate} -I -B -m lu_machinery.review.contract_canary check" in output
    assert f"{sys.executable} -I -B -m lu_machinery.review.contract_canary check" not in output
    assert output.count("systemd-run --user") == 4
    assert list(runner_roots.iterdir()) == []
    assert canary.main(["run", "--head", "a" * 40]) == 2
    assert "outside a dispatched worker" in capsys.readouterr().err
    monkeypatch.setenv(canary.WHEELHOUSE_ENV, "relative/wheelhouse")
    assert canary.main(["run", "--head", "a" * 40, "--dry-run"]) == 2
    assert "must be the absolute path of an existing wheelhouse" in capsys.readouterr().err
    monkeypatch.delenv(canary.WHEELHOUSE_ENV)
    assert canary.main(["run", "--head", "a" * 40, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "--only-binary=:all: -r " in output and f"{canary.WHEELHOUSE_ENV} is unset" in output
    # A product head's dispatcher would be the installed wheel, which nothing can attest.
    assert canary.main(["run", "--repo", "public", "--head", "a" * 40, "--dry-run"]) == 2
    assert "only machinery heads can be canaried" in capsys.readouterr().err
    assert list(runner_roots.iterdir()) == []


@pytest.mark.parametrize("failure", [
    None, "route", "check", "mount", "unmount", "unmount-noop", "interrupt", "unit-lost", "client-interrupt", "submit-interrupt",
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
        fake_setup(argv, cwd)
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
            assert argv[:5] == [str(cwd / "consumer/bin/python"), "-I", "-B", "-m",
                               "lu_machinery.review.contract_canary"]
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
    if failure == "submit-interrupt":
        real_submit = canary.ThreadPoolExecutor.submit

        def interrupted_submit(self, *args, **kwargs):
            real_submit(self, *args, **kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr(canary.ThreadPoolExecutor, "submit", interrupted_submit)
    if failure == "client-interrupt":
        real_result = canary.Future.result

        def interrupted_result(self, *args, **kwargs):
            monkeypatch.setattr(canary.Future, "result", real_result)
            raise KeyboardInterrupt

        monkeypatch.setattr(canary.Future, "result", interrupted_result)
    expected = 0 if failure is None else 1 if failure in {"route", "check"} else 2
    if failure == "client-interrupt":
        with pytest.raises(KeyboardInterrupt):
            canary.main(["run", "--head", "b" * 40])
    else:
        assert canary.main(["run", "--head", "b" * 40]) == expected
    output = capsys.readouterr()
    if failure in {None, "route", "check"}:
        assert sum(cmd[0] == "systemd-run" for cmd in commands) == 4
        assert "squashfs sha256:" in output.out
        assert "claude: FAIL" in output.out if failure == "route" else "claude: PASS" in output.out
    if failure in {"unmount", "unmount-noop", "interrupt", "unit-lost", "submit-interrupt"}:
        assert mounted
        assert "retained" in output.err
        assert len(list(runner_roots.iterdir())) == 1
    else:
        assert not mounted
        assert list(runner_roots.iterdir()) == []
    if failure not in {"mount", "unmount", "unmount-noop", "interrupt", "unit-lost", "submit-interrupt"}:
        assert sum("-u" in cmd for cmd in commands) == 1


def test_runner_starts_all_routes_before_waiting_for_settlement(runner_roots, monkeypatch):
    """Serial route waits deadlock this barrier; cleanup must follow every settlement."""
    from threading import Barrier

    routes = canary.required_routes()
    started = []
    settled = []
    barrier = Barrier(len(routes), timeout=5)
    cleanup = []

    def invoke(argv, *, cwd, env, check=True):
        output = ""
        fake_setup(argv, cwd)
        if argv[0] == "systemd-run":
            route = argv[argv.index("--agent") + 1]
            started.append(route)
            barrier.wait()
            assert set(started) == set(routes)
            settled.append(route)
            output = "CANARY-ROUTE-SETTLED\n"
        if "check" in argv or "-u" in argv:
            assert set(settled) == set(routes)
        if "-u" in argv:
            cleanup.append(argv)
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(canary, "_invoke", invoke)
    monkeypatch.setattr(canary.os.path, "ismount", lambda path: not cleanup)
    assert canary.main(["run", "--head", "b" * 40]) == 0
    assert len(cleanup) == 1
    assert list(runner_roots.iterdir()) == []


@pytest.mark.parametrize("bad_args", [
    ["--head", "main"], ["--head", "c" * 40, "--hard-timeout", "0"],
])
def test_runner_rejects_unpinned_or_unbounded_runs(runner_roots, monkeypatch, bad_args):
    """No task or image may be created for a ref or an unlimited provider run."""
    monkeypatch.setattr(canary, "_invoke", lambda *a, **kw: pytest.fail("invalid run executed"))
    assert canary.main(["run", *bad_args]) == 2
    assert list(runner_roots.iterdir()) == []


@pytest.mark.parametrize(("tree_gib", "used_gib", "free_gib", "starts"), [
    (1, 85, 15, True),  # 2 GiB tree + 0.5 GiB venv allowance: 87.5% used afterwards
    (3, 85, 15, False),  # 6.5 GiB would end at 91.5%
    (0, 90, 10, False),  # already at the refuse level
    (None, 0, 0, False),  # an unreadable disk is unknown, never room
])
def test_runner_never_starts_a_run_that_would_fill_the_disk_past_dispatch_refusal(
    runner_roots, monkeypatch, capsys, tree_gib, used_gib, free_gib, starts,
):
    """Defect class: a canary whose clone and image push the host disk to the level where every dispatch is refused."""
    def usage(path):
        if tree_gib is None:
            raise PermissionError("statvfs denied")
        return SimpleNamespace(used=used_gib * GIB, free=free_gib * GIB)

    commands = []

    def invoke(argv, *, cwd, env, check=True):
        commands.append(argv)
        if "ls-tree" in argv:
            assert argv[argv.index("-C") + 1] == str(runner_roots.parent / "registered-source")
            tree = f"100644 blob {'e' * 40} {2 * (tree_gib or 0) * GIB // 4}\tbig.bin\0"
            return subprocess.CompletedProcess(argv, 0, tree * 2 + f"160000 commit {'f' * 40}       -\tsub\0", "")
        raise canary.CanaryError("setup reached")

    monkeypatch.setattr(shutil, "disk_usage", usage)
    monkeypatch.setattr(canary, "_invoke", invoke)
    assert canary.main(["run", "--head", "b" * 40]) == 2
    err = capsys.readouterr().err
    assert ("setup reached" in err) is starts
    assert ("dispatch admission refuses at 90%" in err) is (tree_gib is not None and not starts)
    assert ("cannot read free space" in err) is (tree_gib is None)
    assert len(commands) == (2 if starts else 1)  # the tree listing, then the first setup step
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


def test_codex_canary_runs_on_the_funded_account_and_refuses_when_none_is(runner_roots, monkeypatch, capsys):
    """Defect class: account-pin starvation. The canary pinned Codex account b, so with b at 0%
    and account a funded the v0.1.67 cut's codex route hit a usage limit and every release was
    blocked until b reset. With no funded account it must refuse, naming the route and accounts."""
    from lu_machinery.fleet import credit_lane

    monkeypatch.setattr(canary, "_invoke", lambda *a, **kw: pytest.fail("planning executed a command"))
    monkeypatch.setattr(credit_lane, "read_routing_budget", lambda **kw: funded_snapshot(a=80.0, b=0.0))
    assert canary.main(["run", "--head", "a" * 40, "--dry-run"]) == 0
    # The plan prints each route's argv; its dispatch arguments follow the embedded bootstrap script.
    codex = next(line for line in capsys.readouterr().out.splitlines() if " --agent codex " in line)
    assert " --codex-account sub " in codex and "b-sub" not in codex  # account a's dispatch label

    monkeypatch.setattr(credit_lane, "read_routing_budget", lambda **kw: funded_snapshot(a=0.0, b=0.0))
    assert canary.main(["run", "--head", "a" * 40, "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "no-allowance: route codex has no account with allowance (b-sub: 0% remaining" in err
    assert "; sub: 0% remaining" in err
    assert list(runner_roots.iterdir()) == []
    with pytest.raises(canary.NoAllowance) as refused:
        canary._run_accounts(funded_snapshot(a=0.0, b=0.0))
    assert (refused.value.route, [label for label, _ in refused.value.readings]) == ("codex", ["b-sub", "sub"])


@pytest.mark.parametrize("outcome", [
    "done", "failed", "interrupted", "dispatch-error", "unknown-wait", "timeout-wait", "recovered-wait",
])
def test_route_bootstrap_records_only_terminal_success_and_cancels_interruption(monkeypatch, capsys, outcome):
    """Unknown dispatch/wait status never signals settlement or records a receipt."""
    import signal
    import sys
    from lu_machinery.dispatcher import cli

    calls = []
    receipts = []

    def main(argv):
        calls.append(argv[0])
        if argv[0] == "dispatch":
            print("canary-task\nnonce-42")
            return 2 if outcome == "dispatch-error" else 0
        assert argv[1:] == ["canary-task", "--run-nonce", "nonce-42"]
        if argv[0] == "wait" and calls.count("wait") == 1:
            if outcome == "interrupted":
                raise KeyboardInterrupt
            if outcome in {"unknown-wait", "recovered-wait"}:
                return 2
            if outcome == "timeout-wait":
                return 124
            return 1 if outcome == "failed" else 0
        if argv[0] == "wait" and outcome in {"unknown-wait", "timeout-wait"}:
            return 2
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
    elif outcome == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            exec(compile(canary._RUN_ROUTE, "<canary-bootstrap>", "exec"), {})
        assert calls == ["dispatch", "wait", "cancel", "wait"]
        assert receipts == []
    else:
        with pytest.raises(SystemExit) as exc:
            exec(compile(canary._RUN_ROUTE, "<canary-bootstrap>", "exec"), {})
        assert exc.value.code == (124 if outcome == "timeout-wait" else 2)
        assert calls == (["dispatch"] if outcome == "dispatch-error" else ["dispatch", "wait", "cancel", "wait"])
        assert receipts == []
    assert ("CANARY-ROUTE-SETTLED" in capsys.readouterr().out.splitlines()) == (
        outcome in {"done", "failed", "interrupted", "recovered-wait"}
    )
