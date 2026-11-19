"""Integration test surfaces for Agy in ab discuss / inbox / lint.

The agent runtime registry already exposes ``agy`` via
``scripts/agent_runtime/registry.py`` (AgyAdapter, ``cli_available:
True``). These tests pin the bridge allowlists so future churn cannot
silently strip ``agy`` from ``ab discuss --with``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots", "bridge_wake_dir", "acp_runs_in_product")

@pytest.fixture
def discuss_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the discussion's Fleet Comms authority writes in this test's plane."""
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(tmp_path / "fleet"))


def test_delegate_dispatch_accepts_agy_agent(tmp_path, monkeypatch, capsys):
    """The v2 parser and dry-run accept AGY regardless of checkout refs."""
    from lu_machinery.dispatcher import cli, git

    task_id = f"test-agy-argparse-probe-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        git, "worktree_plan", lambda *args, **kwargs: (tmp_path / task_id, f"agy/{task_id}", "a" * 40),
    )
    result = cli.main(
        [
            "dispatch",
            "--agent",
            "agy",
            "--model",
            "gemini-4-hydrogen-medium",
            "--task-id",
            task_id,
            "--prompt",
            "noop",
            # #9275: agy without a Ukrainian classification is the bounded fallback.
            "--research-task-family",
            "ukrainian-authoring",
            # This checks the write-mode argv; read-only AGY runs inside the bubblewrap boundary (C-3027).
            "--mode",
            "workspace-write",
            "--dry-run",
        ],
    )
    captured = capsys.readouterr()
    assert result == 0, f"dispatch --agent agy failed:\n{captured.out}\n{captured.err}"
    assert task_id in captured.out
    plan = json.loads(captured.out)
    assert Path(plan["record_path"]).name == f"{task_id}.json"
    assert plan["argv"][0] == "agy"
    assert plan["argv"][plan["argv"].index("--model") + 1] == "gemini-4-hydrogen-medium"


def test_ab_discuss_accepts_agy_in_multi_participant_list(discuss_bridge, monkeypatch):
    """ACP discussion accepts Agy as one member of a bounded participant set."""
    from lu_machinery.ai_agent_bridge import _discuss

    observed: dict[str, object] = {}

    def fake_discussion(**kwargs):
        observed.update(kwargs)
        return {
            "conversation_id": "conversation_" + ("a" * 32),
            "state": "COMPLETE",
            "rounds_completed": 1,
            "synthesis": "ACP synthesis",
        }

    monkeypatch.setattr("lu_machinery.agent_runtime.acpx_discuss.run_discussion", fake_discussion)

    args = SimpleNamespace(
        channel="agy-topic",
        body="Does STRICT grounding measure model discipline fairly?",
        with_agents="agy,pool",
        max_rounds=1,
        review=False,
        review_profile="ukrainian",
        models=None,
    )

    assert _discuss.handle_discuss(args) == 0
    assert observed["participants"] == ("agy", "pool")
    assert observed["models"] is None


def test_ab_discuss_passes_registered_agy_model_pin(discuss_bridge, monkeypatch):
    """The registered Agy ACP model pin reaches the durable controller."""
    from lu_machinery.ai_agent_bridge import _discuss

    observed: dict[str, object] = {}

    def fake_discussion(**kwargs):
        observed.update(kwargs)
        return {
            "conversation_id": "conversation_" + ("b" * 32),
            "state": "COMPLETE",
            "rounds_completed": 1,
            "synthesis": "ACP synthesis",
        }

    monkeypatch.setattr("lu_machinery.agent_runtime.acpx_discuss.run_discussion", fake_discussion)

    args = SimpleNamespace(
        channel="agy-topic",
        body="STRICT bakeoff fairness?",
        with_agents="agy,pool",
        max_rounds=1,
        review=False,
        review_profile="ukrainian",
        models="agy:gemini-4-neon-high",
    )

    assert _discuss.handle_discuss(args) == 0
    assert observed["models"] == {"agy": "gemini-4-neon-high"}
