"""Offline model-boundary checks for the native Codex diagnostic probe."""
import subprocess
from unittest.mock import patch

import pytest

from lu_machinery.agent_runtime import codex_hook_probe as probe
from lu_machinery.review import model_catalog


@pytest.fixture(autouse=True, params=["test-probe-sol", "test-probe-next"])
def probe_catalog(request, monkeypatch):
    model = request.param
    catalog = {
        "seats": {"openai_frontier": {"model_id": model}},
        "models": {model: {"lifecycle": "active", "transports": ["native_codex"]}},
    }
    monkeypatch.setattr(model_catalog, "load_model_catalog", lambda: catalog)
    return catalog


@pytest.mark.parametrize("explicit", [False, True])
def test_probe_pins_sol_low(tmp_path, explicit, probe_catalog, monkeypatch):
    expected = probe_catalog["seats"]["openai_frontier"]["model_id"]
    monkeypatch.delenv("CODEX_HOOK_PROBE_MODEL", raising=False)
    monkeypatch.setattr("sys.argv", ["probe"])
    assert probe.parse_args().model == expected
    model = expected if explicit else None
    with patch.object(probe, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
        probe._run_case(tmp_path, probe.PROBE_CASES[0], model, 1)
    command = run.call_args.args[0]
    assert command[command.index("--model") + 1] == expected
    assert 'model_reasoning_effort="low"' in command


@pytest.mark.parametrize("model", ["test-retired", "test-unknown", ""])
def test_probe_rejects_explicit_old_pin_before_process(tmp_path, model):
    with (
        patch.object(probe, "_run") as run,
        pytest.raises(model_catalog.ModelCatalogError, match="codex_model_not_approved"),
    ):
        probe._run_case(tmp_path, probe.PROBE_CASES[0], model, 1)
    run.assert_not_called()


@pytest.mark.parametrize("source", ["environment", "argument", "catalog"])
def test_probe_rejects_environment_old_pin(monkeypatch, probe_catalog, source):
    monkeypatch.delenv("CODEX_HOOK_PROBE_MODEL", raising=False)
    if source == "environment":
        monkeypatch.setenv("CODEX_HOOK_PROBE_MODEL", "test-retired")
    elif source == "catalog":
        model = probe_catalog["seats"]["openai_frontier"]["model_id"]
        probe_catalog["models"][model]["lifecycle"] = "retired"
    monkeypatch.setattr("sys.argv", ["probe", "--model", "test-retired"] if source == "argument" else ["probe"])
    with pytest.raises(SystemExit) as exc:
        probe.parse_args()
    assert exc.value.code == 2
