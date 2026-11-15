"""Operator-approved account selection and meter arithmetic checks."""

import dataclasses
import json
import pytest
from lu_machinery.agent_runtime.codex_accounts import CodexAccountRefused
from lu_machinery.dispatcher import admission, cli, store, worker
from lu_machinery.dispatcher.environment import child_env
from lu_machinery.dispatcher.types import TaskSpec
from tests.helpers.product_tree import configured_roots  # noqa: F401






def test_label_selection_and_ambient_conflict(tmp_path, monkeypatch):
    api = tmp_path / "api"
    api.mkdir(mode=0o700)
    binding = tmp_path / "codex_accounts.json"
    binding.write_text(json.dumps({"schema_version": 1, "accounts": {
        "api": {"codex_home": str(api), "budget_usd": 200}}}))
    binding.chmod(0o600)
    monkeypatch.setenv("LU_CODEX_ACCOUNTS_FILE", str(binding))
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    assert admission.codex_account("codex", None) == {"codex_account": "sub"}
    assert admission.codex_account("codex", "sub") == {"codex_account": "sub"}
    assert admission.codex_account("codex", "api") == {"codex_account": "api"}
    args = cli.build_parser().parse_args(["dispatch", "--agent", "codex", "--model", "gpt-6-luna", "--codex-account", "api"])
    assert args.codex_account == "api"
    spec = TaskSpec("account", "codex", "gpt-6-luna", "high", "read-only", tmp_path,
                    tmp_path / "prompt", tmp_path / "result", codex_account="api")
    env = child_env(spec)
    assert env["CODEX_HOME"] == str(api)
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"} & env.keys()
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    spec.prompt_file.write_text("fixture prompt")
    record = store.new_record(spec, {"key": "public", "github": "example/repo"}, "codex/account", "a" * 40, {"research_owned_path": []})
    assert record["codex_account"] == "api"
    assert worker._spec(record, {}).codex_account == "api"
    monkeypatch.setenv("CODEX_HOME", str(api))
    assert child_env(spec)["CODEX_HOME"] == str(api)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(CodexAccountRefused, match="codex_account_ambient_conflict"):
        admission.codex_account("codex", "api")
    monkeypatch.delenv("CODEX_HOME")
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        monkeypatch.setenv(name, "fixture-credential")
        with pytest.raises(CodexAccountRefused, match="codex_account_ambient_conflict"):
            admission.codex_account("codex", "api")
        with pytest.raises(CodexAccountRefused, match="codex_account_ambient_conflict"):
            child_env(spec)
        # The subscription default retains today's environment behaviour.
        assert admission.codex_account("codex", None) == {"codex_account": "sub"}
        if name != "CODEX_ACCESS_TOKEN":
            assert child_env(dataclasses.replace(spec, codex_account="sub"))[name] == "fixture-credential"
        monkeypatch.delenv(name)
    with pytest.raises(CodexAccountRefused, match="codex_account_unknown"):
        admission.codex_account("codex", "API")
    with pytest.raises(CodexAccountRefused, match="codex_account_seat_unsupported"):
        admission.codex_account("claude", "api")


def test_unknown_or_unbound_codex_account_never_falls_back(tmp_path, monkeypatch):
    """TEST-DEFAULT-1103: a route that is unknown, unbound, shared or logged out refuses, never substitutes."""
    homes = {label: tmp_path / label for label in ("api", "b-sub", "b-api")}
    for home in homes.values():
        home.mkdir(mode=0o700)
    binding = tmp_path / "codex_accounts.json"

    def bind(accounts):
        binding.write_text(json.dumps({"schema_version": 1, "accounts": {
            label: {"codex_home": str(home), "budget_usd": 200} for label, home in accounts.items()}}))
        binding.chmod(0o600)

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "codex").write_text('#!/bin/sh\n[ "$1 $2" = "login status" ] && [ -f "$CODEX_HOME/fixture-login" ]\n')
    (fake / "codex").chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setenv("LU_CODEX_ACCOUNTS_FILE", str(binding))
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    spec = TaskSpec("account", "codex", "gpt-6-luna", "high", "read-only", tmp_path,
                    tmp_path / "prompt", tmp_path / "result", codex_account="b-sub")

    def refused(code, label="b-sub"):
        with pytest.raises(CodexAccountRefused, match=code):
            admission.codex_account("codex", label)
        with pytest.raises(CodexAccountRefused, match=code):
            child_env(dataclasses.replace(spec, codex_account=label))

    for label in ("c", "B", "", "default", "a", "b-SUB", "c-sub", "b-api "):
        refused("codex_account_unknown", label)
    assert admission.codex_account("codex", "a-sub") == {"codex_account": "sub"}
    bind({"api": homes["api"]})
    for label in ("b-sub", "b", "b-api"):
        refused("codex_account_unbound", label)
    for label in ("api", "a-api"):
        assert admission.codex_account("codex", label) == {"codex_account": "api"}
        assert child_env(dataclasses.replace(spec, codex_account=label))["CODEX_HOME"] == str(homes["api"])
    bind({"api": homes["api"], "b-sub": homes["api"]})
    refused("codex_account_binding_shared")
    bind(homes)
    for label in ("b-sub", "b-api"):
        refused("codex_account_not_logged_in", label)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    refused("codex_account_login_unverified")
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    for label in ("b-sub", "b-api"):
        (homes[label] / "fixture-login").touch()
        assert admission.codex_account("codex", label) == {"codex_account": label}
        assert child_env(dataclasses.replace(spec, codex_account=label))["CODEX_HOME"] == str(homes[label])
    assert admission.codex_account("codex", "b") == {"codex_account": "b-sub"}
    homes["b-sub"].chmod(0o755)
    refused("codex_account_home_not_private")
    homes["b-sub"].chmod(0o700)
    bind({**homes, "b-sub": tmp_path / "missing"})
    refused("codex_account_home_unavailable")


pytestmark = pytest.mark.usefixtures("configured_roots")
