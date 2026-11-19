"""DeepSeek V4.1 Flash runs on an explicit endpoint, refused before spawn when it cannot be paid for (#12376)."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lu_machinery.api import model_stats_rows, subscription_usage as usage
from lu_machinery.dispatcher import checks, cli, git, runtime_context, store, worker, write_boundary
from lu_machinery.dispatcher.providers import opencode
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.orchestration import dispatch_isolation
from lu_machinery.review.reviewer_resolver import REVIEW_CANDIDATES
from tests.helpers.product_tree import configured_roots  # noqa: F401

OPENROUTER_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"
US_PROVIDERS = ["fireworks/us", "baseten/us", "inference-net/us"]  # CTO allow-list, model-assignment.md
SAVANT_FLAGS = ("--agent", "opencode", "--model", "subq-savant-1.4", "--provider", "subq", "--research-role", "test")


@pytest.fixture
def spec(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Reply with exactly: ready\n", encoding="utf-8")
    return TaskSpec("oc", "opencode", "deepseek-v4.1-flash", "high", "read-only", tmp_path, prompt, tmp_path / "final")


@pytest.fixture
def funded(monkeypatch):
    """Both endpoints receive only presence and balance from the private boundary."""
    keys = {"deepseek": True, "openrouter": True}
    balances = {"deepseek": 3.0, "openrouter": 7.0}
    module = SimpleNamespace(
        api_key_present=lambda provider: bool(keys[provider]),
        prepaid_remaining_usd=lambda provider, **kw: balances[provider],
    )
    monkeypatch.setattr(usage, "_private_usage", lambda: module)
    monkeypatch.setattr(opencode, "_resolved_config", lambda env: json.loads(opencode._OPENROUTER_CONFIG))
    monkeypatch.setattr(opencode, "_project_config", lambda *a: [])
    return SimpleNamespace(keys=keys, balances=balances)


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    # The dispatch repository is public; every configured private repository resolves elsewhere.
    monkeypatch.setattr(git, "resolve_repo", lambda key: (
        (tmp_path, dict(key="public", github="example/public")) if key in (None, "public")
        else (tmp_path / "private" / key, dict(key=key, github=f"example/{key}"))))
    monkeypatch.setattr(git, "worktree_plan", lambda *a: (tmp_path, "deepseek/oc", "a" * 40))
    monkeypatch.setattr(git, "create_worktree", Mock())
    monkeypatch.setattr(checks, "check_resources", lambda _: None)
    start = Mock(side_effect=lambda spec, record, prompt, timeout: store.create(
        {**record, "pid": os.getpid(), "process_start": worker._identity(os.getpid())}, prompt,
    ))
    monkeypatch.setattr(worker, "start", start)

    def invoke(*flags, expected=0, task_id="oc", refusal=None):
        before = {record["task_id"] for record in store.list_tasks()}
        assert cli.main(["dispatch", "--agent", "deepseek", "--model", "deepseek-v4.1-flash", "--task-id", task_id,
                         "--mode", "read-only", "--prompt", "Reply with exactly: ready", *flags]) == expected
        if expected:
            start.assert_not_called()
            git.create_worktree.assert_not_called()
            new = [record for record in store.list_tasks() if record["task_id"] not in before]
            assert store.read(task_id, missing_ok=True) is None
            if refusal is None:
                assert new == []
            else:
                [record] = new
                assert (record["requested_task_id"], record["status"], record["never_started_reason"]) == (
                    task_id, "failed", refusal)
                assert record["provider_pgid"] is None and record["usage"] is None
                assert "admission_hold" not in record and "prompt_file" not in record
            return None
        return store.read(task_id)

    return invoke


def test_openrouter_build_pins_v41_flash_and_allow_list(spec, funded, dispatch):
    invocation = opencode.build(replace(spec, provider="openrouter"), {})
    argv = invocation.argv
    assert argv[argv.index("--model") + 1] == OPENROUTER_MODEL
    config = json.loads(invocation.env["OPENCODE_CONFIG_CONTENT"])
    assert config["small_model"] == OPENROUTER_MODEL  # the title call never reaches another model
    assert config["enabled_providers"] == ["openrouter"]  # no other provider is reachable
    assert {agent["model"] for agent in config["agent"].values()} == {OPENROUTER_MODEL}
    openrouter = config["provider"]["openrouter"]
    assert openrouter["whitelist"] == list(openrouter["models"]) == ["deepseek/deepseek-v4.1-flash"]
    model = openrouter["models"]["deepseek/deepseek-v4.1-flash"]
    routing = {"only": US_PROVIDERS, "allow_fallbacks": False, "data_collection": "deny"}
    assert model["id"] == "deepseek/deepseek-v4.1-flash"
    assert model["options"]["provider"] == model["variants"]["high"]["provider"] == routing
    assert json.loads(invocation.env["OPENCODE_PERMISSION"])["edit"] == "deny"  # read-only lock unchanged
    assert dispatch("--provider", "openrouter")["provider"] == "openrouter"


def test_default_build_stays_direct_api(spec, funded, dispatch):
    for provider in (None, "deepseek"):
        invocation = opencode.build(replace(spec, provider=provider), {})
        assert invocation.argv[invocation.argv.index("--model") + 1] == "deepseek/deepseek-flash"
        assert "OPENCODE_CONFIG_CONTENT" not in invocation.env
    assert dispatch()["provider"] == "deepseek"


@pytest.mark.parametrize("selected", ["deepseek", "openrouter"])
def test_missing_endpoint_access_refused_before_spawn(funded, dispatch, capsys, selected):
    funded.keys[selected] = None
    dispatch("--provider", selected, expected=2, refusal="route_eligibility")
    err = capsys.readouterr().err
    assert f"opencode_endpoint_no_access: {selected} has no configured API key" in err
    assert "stub-" not in err


@pytest.mark.parametrize("selected", ["deepseek", "openrouter"])
@pytest.mark.parametrize("balance", [0.99, None])
def test_below_floor_refused_before_spawn(funded, dispatch, capsys, selected, balance):
    funded.balances[selected] = balance
    dispatch("--provider", selected, expected=2, refusal="route_eligibility")
    err = capsys.readouterr().err
    if balance is None:
        assert f"opencode_endpoint_balance_unknown: {selected} balance could not be read" in err
    else:
        assert f"opencode_endpoint_below_floor: {selected} balance $0.99 is below the $1.00 floor" in err
    other = ({"deepseek", "openrouter"} - {selected}).pop()
    funded.balances[other] = 1.00  # the other endpoint exactly at its floor is admitted, never substituted
    assert opencode.endpoint_refusal(other) is None


@pytest.mark.parametrize("provider", ["openrouter/deepseek", "DeepSeek", "native"])
def test_unknown_provider_refused(spec, funded, dispatch, capsys, provider):
    with pytest.raises(ValueError, match="opencode_endpoint_unknown"):
        opencode.build(replace(spec, provider=provider), {})
    dispatch("--provider", provider, expected=2)
    assert "opencode_endpoint_unknown" in capsys.readouterr().err


@pytest.mark.parametrize("provider", ["deepseek", "openrouter"])
def test_endpoint_absent_private_binding_refuses_without_credential_fallback(monkeypatch, provider):
    monkeypatch.setattr(usage, "_private_usage", lambda: None)
    assert opencode.endpoint_refusal(provider) == (
        f"opencode_endpoint_balance_unknown: {provider} balance could not be read"
    )


def test_savant_pins_wire_options_and_records_effort(spec, dispatch, monkeypatch, tmp_path):
    key_file = tmp_path / "test.key"
    key_file.write_text("fixture-key\n", encoding="ascii")
    key_file.chmod(0o600)
    monkeypatch.setenv("LU_SUBQ_API_KEY_FILE", str(key_file))
    monkeypatch.setattr(opencode, "_savant_runtime_refusal", lambda env: None)
    head = "a" * 40
    record = dispatch(*SAVANT_FLAGS, "--data-class", "public-code", "--research-role", "advisory",
                      "--pinned-head", head, task_id="oc-public")
    assert (record["provider"], record["model"], record["effort"]) == ("subq", "subq-savant-1.4", "high")
    trial = replace(spec, provider=record["provider"], model=record["model"], effort=record["effort"],
                    options=record["options"])
    invocation = runtime_context.build(trial, {"DEEPSEEK_API_KEY": "other-fixture-key"})
    config = json.loads(invocation.env["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == config["small_model"] == "subq/subq-savant-1.4"
    assert config["enabled_providers"] == ["subq"]
    # A public class reads the boundary's snapshot of the published start commit with the read-only file tools
    # only; every other tool and any path outside the snapshot stay denied.
    assert config["permission"] == {"*": "deny", "external_directory": "deny", "read": "allow", "grep": "allow",
                                    "glob": "allow"}
    assert invocation.argv[invocation.argv.index("--dir") + 1] == write_boundary.SNAPSHOT_INSIDE
    provider = config["provider"]["subq"]
    assert provider["env"] == ["SUBQ_API_KEY"]
    assert provider["options"] == {"baseURL": "https://api.subq.ai/v1"}
    model = provider["models"]["subq-savant-1.4"]
    assert model["options"] == {"reasoningEffort": "high"}
    assert model["limit"] == {"context": 4_000_000, "output": 64_000}
    assert invocation.env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] == "64000"
    assert "DEEPSEEK_API_KEY" not in invocation.env
    assert "fixture-key" not in invocation.env["OPENCODE_CONFIG_CONTENT"]
    assert invocation.stdin == b"Reply with exactly: ready\n"
    assert all("ready" not in arg for arg in invocation.argv)
    override = opencode.build(replace(trial, options={**trial.options, "max_tokens": "12345"}), {})
    assert override.env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] == "12345"
    private = dispatch(*SAVANT_FLAGS, "--data-class", "private-code", "--repo", "infra-private",
                       "--effort", "max", task_id="oc-private-max")
    assert private["effort"] == "max"
    private_trial = replace(trial, task_id="oc-private-max", effort=private["effort"], options=private["options"])
    invocation = runtime_context.build(private_trial, {})
    private_config = json.loads(invocation.env["OPENCODE_CONFIG_CONTENT"])
    model = private_config["provider"]["subq"]["models"]["subq-savant-1.4"]
    assert model["options"] == {"store": False, "reasoningEffort": "max"}
    # A private class keeps every tool disabled: the prompt is its only input.
    assert private_config["permission"] == {"*": "deny"}
    assert invocation.argv[invocation.argv.index("--dir") + 1] == "/tmp"
    assert invocation.stdin == b"Reply with exactly: ready\n"
    store.record_path(record["task_id"]).with_suffix(".result").write_text(json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "correct", "explanation": "No findings.", "confidence": 1},
        "findings": [],
    }), encoding="utf-8")
    store.replace({**record, "status": "done"})
    store.replace({**private, "status": "failed"})
    rows, _ = model_stats_rows.read_rows(tmp_path)
    assert {(row["model"], row["effort"], row["status"]) for row in rows} == {
        ("subq-savant-1.4", "high", "done"), ("subq-savant-1.4", "max", "failed"),
    }
    review = next(row for row in rows if row["task_id"] == record["task_id"])
    assert review["review"] == {"verdict": "correct", "priorities": {}}
    assert review["pinned_head"] == head and review["pr"] is None
    spec.prompt_file.write_text("Do not send fixture-key", encoding="utf-8")
    with pytest.raises(ValueError, match="savant_prompt_refused"):
        runtime_context.build(private_trial, {})


def test_savant_uses_host_key_file_when_worker_has_no_override(spec, tmp_path, monkeypatch, dispatch):
    key_file = tmp_path / "configured key"
    key_file.write_text("fixture-key\n", encoding="ascii")
    key_file.chmod(0o600)
    monkeypatch.delenv("LU_SUBQ_API_KEY_FILE", raising=False)
    monkeypatch.delenv("SUBQ_API_KEY", raising=False)
    monkeypatch.setattr(opencode, "_savant_runtime_refusal", lambda env: None)
    monkeypatch.setattr(dispatch_isolation, "user_manager_env", lambda env: env)
    manager = Mock(return_value=SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"type": "as", "data": [f"LU_SUBQ_API_KEY_FILE={key_file}"]}).encode(),
    ))
    monkeypatch.setattr(opencode, "subprocess", SimpleNamespace(**{**vars(opencode.subprocess), "run": manager}))

    record = dispatch(*SAVANT_FLAGS, "--data-class", "public-code")
    trial = replace(spec, provider=record["provider"], model=record["model"], options=record["options"])
    invocation = runtime_context.build(trial, {})

    assert invocation.env["SUBQ_API_KEY"] == "fixture-key"
    assert "SUBQ_API_KEY" not in os.environ
    assert b"fixture-key" not in invocation.stdin
    assert "fixture-key" not in invocation.env["OPENCODE_CONFIG_CONTENT"]
    record_bytes = store.record_path(record["task_id"]).read_bytes()
    assert b"fixture-key" not in record_bytes
    assert os.fsencode(key_file) not in record_bytes
    assert manager.call_count == 2


def test_savant_refuses_secrets_or_unknown_data_before_spawn(dispatch, capsys):
    for flags in (("--data-class", "secret"), ("--data-class", "credentials"), ("--data-class", "unknown"), ()):
        dispatch(*SAVANT_FLAGS, *flags, expected=2, refusal="route_eligibility")
        assert "savant_data_class_refused" in capsys.readouterr().err
    dispatch(*SAVANT_FLAGS, "--data-class", "public-code", "--repo", "infra-private", expected=2,
             refusal="route_eligibility")
    assert "savant_repository_refused" in capsys.readouterr().err
    # A private class reads no files, so naming paths is refused at dispatch, before any record exists.
    dispatch(*SAVANT_FLAGS, "--data-class", "private-code", "--repo", "infra-private", "--read-path", "docs",
             expected=2)
    assert "savant_read_path_refused" in capsys.readouterr().err


def test_savant_refuses_review_of_record_before_spawn(dispatch, capsys):
    assert all(candidate.concrete_model != "subq-savant-1.4" for candidate in REVIEW_CANDIDATES.values())
    dispatch(*SAVANT_FLAGS, "--data-class", "public-code", "--risk", "high", "--require-review-verdict", expected=2,
             refusal="route_eligibility")
    assert "holds no review, critique or approval role" in capsys.readouterr().err
    dispatch(*SAVANT_FLAGS, "--data-class", "public-code", "--mode", "workspace-write", expected=2,
             refusal="route_eligibility")
    assert "savant_role_refused: a write run needs --research-role implementation" in capsys.readouterr().err
    dispatch(*SAVANT_FLAGS, "--data-class", "public-code", "--effort", "medium", expected=2,
             refusal="route_eligibility")
    assert "does not offer effort 'medium'" in capsys.readouterr().err


pytestmark = pytest.mark.usefixtures("configured_roots")


def _published_repository(tmp_path, files, links=()):
    """A fixture repository whose commit is on ``refs/remotes/origin/main``, as a fetched public branch is."""
    import subprocess

    repository = tmp_path / "public"
    for name, data in files.items():
        (repository / name).parent.mkdir(parents=True, exist_ok=True)
        (repository / name).write_bytes(data)
    for name, target in links:
        (repository / name).symlink_to(target)

    def run(*args):
        return subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True,
                              text=True, timeout=60, env={"PATH": os.defpath, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
                                              "GIT_CONFIG_GLOBAL": os.devnull}).stdout.strip()

    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "fixture")
    commit = run("rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, timeout=60)
    run("remote", "add", "origin", remote.as_uri())
    run("push", "-q", "origin", "main")
    return repository, commit, run


def test_savant_snapshot_holds_only_published_regular_files(tmp_path, monkeypatch):
    """Defect class: a public Savant run's tools reach Git metadata, secrets, links or unpublished work (C2, C4)."""
    secret = b"EXCLUDED-CONTENT"
    files = {
        "src/allowed.py": b"ALLOWED = 1\n", "docs/guide.md": b"# Guide\n",
        ".env": secret, "config/.env.local": secret, "keys/id_ed25519": secret, "keys/server.pem": secret,
        "secrets/token.txt": secret, "app/credentials.json": secret, ".ssh/config": secret, "deploy/site.key": secret,
        "AGENTS.md": secret, "nested/CLAUDE.md": secret, "opencode.json": secret, ".opencode/agent.md": secret,
        "docs/CONTEXT.md": secret, "home/auth.json": secret, "home/.credentials.json": secret, "prod.env": secret,
        ".gitconfig": secret, ".mcp.json": secret, "config/settings.ini": b"api = opaque-fixture-value-0123456789\n",
        "config/subq.txt": b"subq = k3y\n", "docs/howto.md": b"Run as fixtureacct.\n",
        "docs/notes.txt": b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + secret,
        "src/token.py": b"TOKEN = 'ghp_" + b"A" * 36 + b"'\n" + secret,
    }
    repository, commit, run = _published_repository(tmp_path, files, links=[("link.py", "src/allowed.py"),
                                                                             ("outside", "/etc")])
    # Only a registered public origin is trusted; the fixture's bare remote stands in for it.
    with pytest.raises(write_boundary.Refused, match="origin is not a registered public repository"):
        write_boundary._snapshot(repository, commit, [], tmp_path / "unregistered", 1024)
    # The fixture's bare remote stands in for GitHub's anonymous listing of a registered public repository.
    monkeypatch.setattr(write_boundary, "_public_origin", lambda _repository: "example/public")
    monkeypatch.setattr(write_boundary, "_remote_tips", lambda slug: {
        ref: oid for oid, ref in (line.split("\t") for line in run("ls-remote", "origin", "refs/heads/*").splitlines())})
    # The host's account names come from its needles file; the test owns that file. Sentence punctuation beside a
    # name is a word boundary ("Run as fixtureacct." is screened).
    needles = tmp_path / "needles.json"
    needles.write_text('{"schema_version": 1, "run_users": ["fixtureacct"], "home_dirs": []}', encoding="utf-8")
    monkeypatch.setenv("LU_OPSEC_NEEDLES_FILE", str(needles))
    # An opaque credential the launcher holds (the run's SubQ key, for one) is screened by its exact value.
    monkeypatch.setenv("FIXTURE_API_KEY", "opaque-fixture-value-0123456789")
    monkeypatch.setenv("SUBQ_API_KEY", "k3y")  # the configured SubQ key, however short
    target = tmp_path / "snapshot"
    info = write_boundary._snapshot(repository, commit, [], target, 1024)
    written = {path.relative_to(target).as_posix(): path for path in target.rglob("*") if path.is_file()}
    assert sorted(written) == ["docs/guide.md", "src/allowed.py"]
    assert not any(path.is_symlink() for path in target.rglob("*"))
    assert all(secret not in path.read_bytes() for path in written.values())
    assert all(path.stat().st_mode & 0o777 == 0o400 for path in written.values())
    # 18 excluded names and 2 links leave before reading; 5 files leave for a key, a token, a known secret or an
    # account name.
    assert info["files"] == 2 and info["left_out"] == 25 and info["commit"] == commit
    # --read-path narrows it to the named directory.
    narrowed = write_boundary._snapshot(repository, commit, ["docs"], tmp_path / "narrow", 1024)
    assert narrowed["files"] == 1 and (tmp_path / "narrow" / "docs" / "guide.md").is_file()
    # Fail closed, each with its reason: a path naming nothing, an over-cap tree and an unpublished commit.
    with pytest.raises(write_boundary.Refused, match="a --read-path names no readable file"):
        write_boundary._snapshot(repository, commit, ["missing"], tmp_path / "missing", 1024)
    with pytest.raises(write_boundary.Refused, match="exceed 10 bytes or 20000 files; name the task's files"):
        write_boundary._snapshot(repository, commit, [], tmp_path / "small", 10)
    # The tree listing itself is bounded, excluded entries included, before any cap is checked.
    listing_cap = write_boundary.MAX_TREE_LISTING_BYTES
    monkeypatch.setattr(write_boundary, "MAX_TREE_LISTING_BYTES", 100)
    with pytest.raises(write_boundary.Refused, match="git ls-tree printed more than 100 bytes"):
        write_boundary._snapshot(repository, commit, [], tmp_path / "listing", 1024)
    monkeypatch.setattr(write_boundary, "MAX_TREE_LISTING_BYTES", listing_cap)
    (repository / "src" / "allowed.py").write_bytes(b"UNPUBLISHED = 1\n")
    run("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qam", "local only")
    with pytest.raises(write_boundary.Refused, match="no branch of the public remote contains the start commit"):
        write_boundary._snapshot(repository, run("rev-parse", "HEAD"), [], tmp_path / "local", 1024)


def test_savant_launch_builds_the_snapshot_and_ceiling_only_for_public_classes(tmp_path, monkeypatch):
    """Defect class: a private Savant run gets file tools, or a public one runs without its bounds (C1, C3)."""
    from lu_machinery.dispatcher.types import Invocation

    monkeypatch.setattr(write_boundary.shutil, "which", lambda *args, **kwargs: "/usr/bin/true")
    base = TaskSpec("oc-launch", "opencode", "subq-savant-1.4", "high", "read-only", tmp_path, tmp_path / "prompt",
                    tmp_path / "final", provider="subq")
    invocation = Invocation(["opencode"], None, {}, None)
    public = write_boundary.launcher(replace(base, options={"data_class": "public-code",
                                                            "read_paths": '["lu_machinery/config"]'}),
                                     invocation, tmp_path / "task.json")
    public = public[:public.index("--")]
    assert public[public.index("--upload-ceiling") + 1] == str(opencode.SAVANT_UPLOAD_MAX_BYTES)
    assert public[public.index("--snapshot-bytes") + 1] == str(opencode.SAVANT_SNAPSHOT_MAX_BYTES)
    assert public[public.index("--read-path") + 1] == "lu_machinery/config"
    private = write_boundary.launcher(replace(base, options={"data_class": "private-code"}), invocation,
                                      tmp_path / "task.json")
    private = private[:private.index("--")]
    assert "--upload-ceiling" in private and "--snapshot-bytes" not in private and "--read-path" not in private
    # No Savant run gets a GitHub relay (C4: GitHub access refused even to the process) or a Sources relay (#898).
    assert all(flag in argv for flag in ("--no-github-relays", "--no-sources-relay") for argv in (public, private))
    # A malformed record's paths are refused, never read as the whole tree.
    with pytest.raises(ValueError, match="savant_read_path_refused"):
        opencode.savant_read_paths({"read_paths": "{}"})


WRITE_FLAGS = (*SAVANT_FLAGS[:-1], "implementation", "--mode", "workspace-write")


def test_savant_write_run_is_admitted_only_public_implementation_with_owned_paths(spec, dispatch, monkeypatch,
                                                                                  tmp_path, capsys):
    """Defect class (#898): a Savant write run starts for a private repository or class, another role, without
    owned paths, or with tools beyond editing its own view; or a read-only run takes the implementation role."""
    key_file = tmp_path / "test.key"
    key_file.write_text("fixture-key\n", encoding="ascii")
    key_file.chmod(0o600)
    monkeypatch.setenv("LU_SUBQ_API_KEY_FILE", str(key_file))
    monkeypatch.setattr(opencode, "_savant_runtime_refusal", lambda env: None)
    # The fixture checkout stands in for the registered public repository's canonical origin.
    monkeypatch.setattr(git, "verification_origin", lambda path: "https://github.com/example/public.git")
    monkeypatch.setattr(git, "canonical_origins", lambda info: {"https://github.com/example/public.git"})
    for flags, reason in (
        (("--research-role", "test", "--data-class", "public-code", "--owned-path", "docs"),
         "a write run needs --research-role implementation"),
        (("--data-class", "private-code", "--repo", "infra-private", "--owned-path", "docs"),
         "savant_write_refused: a write run needs --data-class public-code or public-docs"),
        (("--data-class", "public-code", "--repo", "infra-private", "--owned-path", "docs"),
         "savant_repository_refused: private repositories require a private data class"),
        (("--data-class", "public-code"), "savant_write_refused: a write run needs --owned-path"),
    ):
        dispatch(*WRITE_FLAGS, *flags, expected=2, refusal="route_eligibility", task_id="oc-write-refused")
        assert reason in capsys.readouterr().err
    dispatch(*SAVANT_FLAGS[:-1], "implementation", "--data-class", "public-code", expected=2,
             refusal="route_eligibility", task_id="oc-read-implementation")
    assert "use --research-role advisory or test" in capsys.readouterr().err
    record = dispatch(*WRITE_FLAGS, "--data-class", "public-code", "--owned-path", "docs", "--read-path", "docs",
                      task_id="oc-write")
    assert (record["mode"], record["owned_paths"], record["options"]["research_role"]) == (
        "workspace-write", ["docs"], "implementation")
    trial = replace(spec, task_id="oc-write", mode="workspace-write", provider=record["provider"],
                    model=record["model"], effort=record["effort"], options=record["options"])
    invocation = runtime_context.build(trial, {})
    config = json.loads(invocation.env["OPENCODE_CONFIG_CONTENT"])
    # Editing its own copy of the view is the only tool beyond the read-only ones; no shell, task or web tool.
    assert config["permission"] == {"*": "deny", "external_directory": "deny", "read": "allow", "grep": "allow",
                                    "glob": "allow", "edit": "allow"}
    # No formatter rewrites the model's edits and no language server starts inside.
    assert config["formatter"] is False and config["lsp"] is False
    assert invocation.argv[invocation.argv.index("--dir") + 1] == write_boundary.SNAPSHOT_INSIDE
    # It always runs inside the boundary, with the patch target and the record's owned paths, and no relay.
    assert worker._in_boundary(trial) and not worker._in_boundary(replace(trial, provider="deepseek"))
    monkeypatch.setattr(write_boundary.shutil, "which", lambda *args, **kwargs: os.devnull)
    task_file = store.record_path("oc-write")
    with pytest.raises(ValueError, match="needs its patch descriptor"):
        write_boundary.launcher(trial, invocation, task_file)
    argv = write_boundary.launcher(trial, invocation, task_file, draft_fd=7)
    argv = argv[:argv.index("--")]
    assert argv[argv.index("--mode") + 1] == "workspace-write"
    # The launcher only stages the patch; settlement keeps it as <task>.patch for a done run.
    assert argv[argv.index("--draft-patch-fd") + 1] == "7"
    assert argv[argv.index("--owned-path") + 1] == "docs" and argv[argv.index("--read-path") + 1] == "docs"
    assert "--no-github-relays" in argv and "--no-sources-relay" in argv and "--snapshot-bytes" in argv
    assert "--sources-mcp" not in argv
    store.replace({**record, "owned_paths": []})
    with pytest.raises(ValueError, match="a Savant write run needs owned paths"):
        write_boundary.launcher(trial, invocation, task_file, draft_fd=7)
    with pytest.raises(ValueError, match="savant_write_refused"):
        opencode.build(replace(trial, options={**trial.options, "data_class": "private-code"}), {})


@pytest.mark.parametrize(("change", "status", "cause"), [
    ({}, "done", None),
    ({"paths": ["docs/a.md", "src/b.py"]}, "no_deliverable", "outside_owned_paths: src/b.py"),
    ({"paths": []}, "no_deliverable", "savant_draft_unowned_or_empty"),
    ({"dirty": True}, "no_deliverable", "savant_draft_worktree_changed_or_unknown"),
    ({"commits": 1}, "no_deliverable", "savant_draft_worktree_changed_or_unknown"),
    ({"provider": "deepseek"}, "no_deliverable", "no_commits_or_unknown"),
    # A malformed path list is refused whole, never read as its well-formed part (review of #919).
    ({"paths": ["docs/a.md", 7]}, "no_deliverable", "savant_draft_unowned_or_empty"),
    # A Savant write delivers only its draft: commits found elsewhere never make it done (review of #919).
    ({"draft": "unchanged", "commits": 1}, "no_deliverable", "savant_draft_missing"),
])
def test_savant_draft_patch_is_the_write_runs_deliverable(change, status, cause):
    """Defect class (#898): settlement rejects a Savant write run's owned-path patch, accepts one with another
    path or a changed worktree, or accepts a draft receipt on any other route."""
    record = {
        "task_id": "oc-write", "agent": "opencode", "provider": change.get("provider", "subq"),
        "mode": "workspace-write", "status": "done", "owned_paths": ["docs"],
        "commits_ahead": change.get("commits", 0), "worktree_dirty_on_exit": change.get("dirty", False),
        "write_boundary": {"import": {"status": change.get("draft", "drafted"), "paths": change.get("paths", ["docs/a.md"])}},
    }
    worker._enforce_delivery(record)
    assert (record["status"], record.get("no_deliverable_reason")) == (status, cause)


@pytest.mark.parametrize(("case", "status", "cause"), [
    ("kept", "done", None),
    ("missing", "no_deliverable", "savant_draft_patch_missing_or_changed"),
    ("changed", "no_deliverable", "savant_draft_patch_missing_or_changed"),
    ("failed-run", "failed", None),
    # A supervisor that died after keeping the patch, before publishing done: crash recovery removes it.
    ("crashed-after-keep", "crashed", None),
])
def test_savant_patch_is_kept_only_for_a_done_run(case, status, cause, tmp_path):
    """Defect class (#898, review of #919): a refused run leaves a patch, or a run settles done with its patch
    missing or different from the one its receipt names."""
    task_file = tmp_path / "oc-write.json"
    patch = b"diff --git a/docs/a.md b/docs/a.md\n"
    # The supervisor's unnamed file, as the launcher left it.
    descriptor = None if case == "missing" else os.open(tmp_path, os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600)
    if descriptor is not None:
        os.write(descriptor, patch + (b"tampered\n" if case == "changed" else b""))
    if case == "crashed-after-keep":
        write_boundary.draft_patch(task_file).write_bytes(patch)
    record = {
        "task_id": "oc-write", "agent": "opencode", "provider": "subq", "mode": "workspace-write",
        "status": {"failed-run": "failed", "crashed-after-keep": "crashed"}.get(case, "done"),
        "write_boundary": {"import": {"status": "drafted", "paths": ["docs/a.md"], "bytes": len(patch),
                                      "patch_sha256": hashlib.sha256(patch).hexdigest()}},
    }
    try:
        worker._settle_savant_patch(record, task_file, descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    assert (record["status"], record.get("no_deliverable_reason")) == (status, cause)
    kept = write_boundary.draft_patch(task_file)
    assert kept.exists() == (status == "done") and sorted(path.name for path in tmp_path.iterdir()) == (
        ["oc-write.patch"] if status == "done" else [])
    if status == "done":
        assert kept.read_bytes() == patch and record["write_boundary"]["import"]["patch"] == str(kept)
