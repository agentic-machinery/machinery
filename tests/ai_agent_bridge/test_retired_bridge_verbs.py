"""The bridge CLI after #515: retired legacy verbs refuse; the kept ask path still works.

The ask-path tests below moved here unchanged from the deleted
test_bridge_inbox_cli.py and test_process_routing.py, whose other tests covered
only removed code.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lu_machinery.agent_runtime.runner import resolve_inter_agent_route
from lu_machinery.ai_agent_bridge import _acp_compat, _cli
from lu_machinery.ai_agent_bridge._acp_compat import registered_participant_model, resolve_compat_model
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots")

# Every verb that read or wrote the legacy stores, with arguments a caller would pass.
RETIRED_CALLS = (
    ("inbox", "--for", "claude"),
    ("inbox", "run", "claude", "--once"),
    ("read", "12"),
    ("read", "12", "--json"),
    ("process-ask", "12", "opencode"),
    ("send", "hello", "--to", "claude"),
    ("ack", "--consumed-by-live-driver", "12"),
    ("ack-all", "claude"),
    ("conversation", "task-1"),
    ("thread", "task-1"),
    ("process", "12"),
    ("process-claude", "12"),
    ("process-codex", "12"),
    ("process-grok", "12"),
    ("process-grok-build", "12"),
    ("process-all",),
    ("process-claude-all",),
    ("process-codex-all",),
    ("channel", "list"),
    ("post", "reviews", "hello", "--to", "claude"),
    ("p", "reviews", "claude", "hello"),
    ("sync", "--all"),
    ("reconcile", "--dry-run"),
    ("cleanup", "--dry-run"),
    ("status",),
    ("converse", "hello", "--task-id", "task-1"),
    ("interactive",),
)


@pytest.mark.parametrize("argv", RETIRED_CALLS, ids=lambda argv: " ".join(argv))
def test_retired_bridge_verbs_refuse_with_fleet_replacement(argv, tmp_path, monkeypatch, capsys):
    """Defect class: a retired verb silently does nothing or touches a store.

    Each call exits 2 before any store is opened, prints nothing on stdout, and
    names its replacement on stderr.
    """
    broker = tmp_path / "messages.db"
    pids = tmp_path / "pids"
    monkeypatch.setenv("AB_DB_PATH", str(broker))
    monkeypatch.setenv("AB_PID_DIR", str(pids))
    monkeypatch.setattr(sys, "argv", ["ab", *argv])
    monkeypatch.setattr(sys, "stdin", None)

    with pytest.raises(SystemExit) as exited:
        _cli.main()

    out = capsys.readouterr()
    assert exited.value.code == 2
    assert out.out == ""
    assert f"ab {argv[0]}" in out.err and "is retired" in out.err
    assert any(name in out.err for name in ("fleet_comms", "/api/fleet/", "ask-"))
    assert not broker.exists()
    assert not pids.exists()


# --- Kept ask-* CLI behaviour (moved unchanged) ---------------------------------


def _run_cli(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["ab", *argv]):
        try:
            _cli.main()
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 0
    return 0


def _clear_identity_env(monkeypatch) -> None:
    for var in (
        "SESSION_HANDOFF_AGENT",
        "CLAUDE_AGENT_NAME",
        "CODEX_SESSION",
        "GROK_AGENT",
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
        "GEMINI_SESSION",
    ):
        monkeypatch.delenv(var, raising=False)


def test_detect_caller_identity_honors_session_handoff_agent(monkeypatch):
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("SESSION_HANDOFF_AGENT", "claude-infra")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/claude-project")

    assert _cli._detect_caller_identity_from_env() == "claude-infra"


def test_detect_caller_identity_phantom_handoff_agent_aliased_to_provider(monkeypatch):
    # #7597: a pre-fix launcher exported SESSION_HANDOFF_AGENT=codex-monitor
    # (grok-monitor is a registered slot since #10835).
    # The explicit marker must still win (aliased to the provider), not fall
    # through to the soft CLAUDE_PROJECT_DIR heuristic.
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("SESSION_HANDOFF_AGENT", "codex-monitor")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/claude-project")

    assert _cli._detect_caller_identity_from_env() == "codex"


def test_explicit_from_mismatch_with_handoff_agent_is_warning_tagged(monkeypatch, capsys):
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("SESSION_HANDOFF_AGENT", "claude-folk")

    sender = _cli._resolve_from_llm(SimpleNamespace(from_llm="claude-infra"))

    assert sender == "claude-infra"
    assert "[SENDER_IDENTITY_MISMATCH]" in capsys.readouterr().err


def test_detect_caller_identity_grok_agent_maps_to_canonical_grok(monkeypatch):
    # The native grok CLI exports GROK_AGENT for every tool shell it spawns
    # (model-independent). It must auto-detect as the canonical `grok` seat
    # (historical alias: grok-build) so a grok session can `ab ask-claude`
    # without --from and get the reply back.
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("GROK_AGENT", "1")

    assert _cli._detect_caller_identity_from_env() == "grok"


def test_detect_caller_identity_grok_agent_beats_soft_claude_heuristic(monkeypatch):
    # A grok session that inherited a stray CLAUDE_* var must still resolve to
    # the canonical `grok` seat, never misroute to the claude inbox.
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("GROK_AGENT", "1")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/claude-project")

    assert _cli._detect_caller_identity_from_env() == "grok"


def test_detect_caller_identity_grok_agent_profile_name_is_not_a_false_positive(monkeypatch):
    # GROK_AGENT doubles as an agent-profile *name* selector; only the tool-shell
    # sentinel value "1" means "spawned by a grok CLI shell". A profile name must
    # NOT be mistaken for the grok-build lane — it falls through to normal detect.
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("GROK_AGENT", "my-custom-agent")

    assert _cli._detect_caller_identity_from_env() is None


def test_detect_caller_identity_explicit_handoff_still_wins_over_grok_agent(monkeypatch):
    # An explicit, validated SESSION_HANDOFF_AGENT is authoritative over the
    # weaker GROK_AGENT marker.
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("GROK_AGENT", "1")
    monkeypatch.setenv("SESSION_HANDOFF_AGENT", "codex")

    assert _cli._detect_caller_identity_from_env() == "codex"


def test_ask_claude_reads_body_from_stdin_on_dash(monkeypatch):
    # Regression for the grok "empty result" bug: ask-claude used args.content
    # directly, so `ask-claude - < prompt.md` sent the literal "-" as the body.
    # Every other ask-* handler resolves "-" to stdin; ask-claude must too.
    captured: dict[str, object] = {}

    def _fake_acp(target, content, **_kwargs):
        captured.update(target=target, content=content)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(_acp_compat, "run_compat_ask", _fake_acp)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: "the real question body"))

    exit_code = _run_cli(["ask-claude", "-", "--task-id", "t-stdin", "--from", "grok-build"])

    assert exit_code == 0
    assert captured["target"] == "claude"
    assert captured["content"] == "the real question body"


def test_ask_claude_literal_content_is_not_treated_as_stdin(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_acp(target, content, **_kwargs):
        captured.update(target=target, content=content)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(_acp_compat, "run_compat_ask", _fake_acp)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: "SHOULD-NOT-BE-USED"))

    exit_code = _run_cli(["ask-claude", "a literal question", "--task-id", "t-lit", "--from", "grok-build"])

    assert exit_code == 0
    assert captured["target"] == "claude"
    assert captured["content"] == "a literal question"


def test_ask_gemini_default_model_is_none_so_registry_pin_applies() -> None:
    """A bare ask-gemini carries no model slug; the route applies the pin."""
    from lu_machinery.ai_agent_bridge import _cli

    parser = _cli._build_parser()
    args = parser.parse_args(["ask-gemini", "hello", "--task-id", "t-1"])
    assert args.model is None
    assert resolve_compat_model("gemini", args.model) is None
    # The route resolver then applies the live pin — and accepts it.
    route = resolve_inter_agent_route("agy")
    assert route.model == registered_participant_model("agy")


def test_resolve_compat_model_tracks_pin_rotation(monkeypatch) -> None:
    """Rotating the registry pin updates the accepted pin and its display alias."""
    from lu_machinery.agent_runtime.adapters.acpx import ACPX_SUPPORTED_PARTICIPANTS

    monkeypatch.setitem(
        ACPX_SUPPORTED_PARTICIPANTS,
        "agy",
        {"seat": "acpx-agy-shadow", "agent": "agy", "model": "gemini-9.9-flash-high"},
    )
    assert registered_participant_model("agy") == "gemini-9.9-flash-high"
    assert resolve_compat_model("gemini", "Gemini 9.9 Flash (High)") == "gemini-9.9-flash-high"
    # Compatibility aliases still follow a rotated pin. A retired concrete
    # generation must not silently upgrade, even when the registry rotates (#9301).
    assert resolve_compat_model("gemini", "gemini-3-flash-preview") == "gemini-9.9-flash-high"
    assert resolve_compat_model("gemini", "gemini-3.0-flash-preview") == "gemini-9.9-flash-high"
    with pytest.raises(ValueError, match=r"gemini-3\.7-flash.*retired.*use gemini-3\.8-flash-high"):
        resolve_compat_model("gemini", "gemini-3.7-flash")
    # Non-Gemini model ids pass through for the route resolver to judge loudly.
    assert resolve_compat_model("gemini", "custom-provider/model-x") == "custom-provider/model-x"
    # Non-agy seats never get rewritten.
    assert resolve_compat_model("cursor", "composer-2") == "composer-2"


def test_default_gemini_model_missing_registry_pin_fails_loudly(monkeypatch) -> None:
    """#6959: missing registry pin fails loudly instead of falling back to stale literal."""
    from lu_machinery.agent_runtime.adapters.acpx import ACPX_SUPPORTED_PARTICIPANTS
    from lu_machinery.ai_agent_bridge._config import default_gemini_model

    monkeypatch.delenv("AB_GEMINI_MODEL", raising=False)
    monkeypatch.setitem(
        ACPX_SUPPORTED_PARTICIPANTS,
        "agy",
        {"seat": "acpx-agy-shadow", "agent": "agy", "model": None},
    )
    with pytest.raises(RuntimeError, match="No default Gemini model configured"):
        default_gemini_model()
