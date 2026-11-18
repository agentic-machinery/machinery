"""The retained Gemma route queues through Fleet without the retired broker."""

from unittest.mock import patch

import pytest

from lu_machinery.ai_agent_bridge._opencode import GEMMA_MODEL, ask_gemma


def test_gemma_uses_fleet_job_without_legacy_message_creation():
    with patch("lu_machinery.ai_agent_bridge._opencode._enqueue_opencode_ask", return_value="authority-job_probe") as send:
        assert ask_gemma("hramatka probe", task_id="t") == "authority-job_probe"
    assert send.call_args.args == ("hramatka probe", "t", "gemma")
    assert send.call_args.kwargs["model"] == GEMMA_MODEL
    assert send.call_args.kwargs["background"] is False


def test_gemma_remains_callable_under_ci(tmp_path, monkeypatch):
    """The kept hramatka route must not gain review or advisory eligibility."""
    from lu_machinery.fleet_comms.authority import AuthorityService

    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(tmp_path / "plane"))
    with patch("lu_machinery.ai_agent_bridge._opencode._invoke_opencode", return_value="hramatka reply") as invoke:
        job_id = ask_gemma("hramatka probe", task_id="t")
        for msg_type in ("review", "advisory"):
            with pytest.raises(ValueError, match="holds no"):
                ask_gemma("hramatka probe", task_id="t", msg_type=msg_type)
    invoke.assert_called_once()
    with AuthorityService() as service:
        assert service.get_job(job_id).state == "complete"
        assert service.store.connection.execute("SELECT COUNT(*) FROM authority_jobs").fetchone()[0] == 1
