"""Agy (Antigravity CLI) interaction: ask_agy and process_for_agy.

Mirrors the codex bridge shape (one-shot send + runtime invoke + response
write-back via the broker). Agy is the Gemini-3.5-Flash-High Antigravity
CLI; docs at https://antigravity.google/docs/cli-overview. Agy is used
for cheap, fast Q&A and short coordination — NOT long-running task
execution. Long-running V7 writer-phase work goes through
``scripts/dispatch.py dispatch --agent agy --model MODEL`` instead.
"""

import json
import os
from pathlib import Path

from lu_machinery.agent_runtime import runner as agent_runner
from lu_machinery.agent_runtime.agy_hook_config import provision_workspace_guard
from lu_machinery.agent_runtime.errors import (
    AgentStalledError,
    AgentTimeoutError,
    AgentUnavailableError,
    RateLimitedError,
)
from lu_machinery.common import json_depth
from lu_machinery.common.scratch import ensure_scratch_root

from . import _config
from ._ask_contract import (
    requested_effort,
    resolve_model_selection,
    response_provenance,
    unsupported_effort_note,
)
from ._ask_lifecycle import launch_background_ask, record_ask_failure, record_ask_reply, register_ask
from ._db import get_db, set_session

# Keep the established imports for installed-release consumers.
from ._gemini_review import (
    AGY_REVIEW_PROFILE_REQUIRED,
    GEMINI_CODE_REVIEW_FORBIDDEN,
    GEMINI_REVIEW_AGENTS,
    GEMINI_REVIEW_PROFILE_CHOICES,
    GeminiChangedPathListError,
    gemini_content_paths_error,
    gemini_model_leaf,
    gemini_pr_or_branch_content_error,
    gemini_review_profile_error,
    gemini_review_targets_model,
    is_content_class_path,
    is_gemini_family_model,
    list_branch_changed_paths,
    list_commit_changed_paths,
    resolve_same_repo_pr_head,
)
from ._messaging import acknowledge, send_message
from ._prompts import build_agy_prompt, require_core_or_exit

__all__ = [
    "AGY_REVIEW_PROFILE_REQUIRED",
    "GEMINI_CODE_REVIEW_FORBIDDEN",
    "GEMINI_REVIEW_AGENTS",
    "GEMINI_REVIEW_PROFILE_CHOICES",
    "GeminiChangedPathListError",
    "ask_agy",
    "gemini_content_paths_error",
    "gemini_model_leaf",
    "gemini_pr_or_branch_content_error",
    "gemini_review_profile_error",
    "gemini_review_targets_model",
    "is_content_class_path",
    "is_gemini_family_model",
    "list_branch_changed_paths",
    "list_commit_changed_paths",
    "process_for_agy",
    "resolve_same_repo_pr_head",
]

_DEFAULT_AGY_BRIDGE_TIMEOUT_SECONDS = 900
_NO_TIMEOUT_AGY_BRIDGE_TIMEOUT_SECONDS = 24 * 60 * 60


def _default_agy_model() -> str:
    """The AGY seat's model, read from the live ACP participant registry.

    Never an independently hardcoded slug: the registry pin is what the route
    resolver enforces, so deriving the default from it makes stale-slug drift
    impossible (#6894).
    """
    from lu_machinery.agent_runtime.adapters.acpx import ACPX_SUPPORTED_PARTICIPANTS

    pin = ACPX_SUPPORTED_PARTICIPANTS["agy"]["model"]
    assert pin is not None  # the agy participant is always pinned
    return pin


def _agy_ask_scratch_cwd() -> Path:
    """Out-of-tree scratch cwd for unsandboxed agy bridge asks.

    Must live OUTSIDE the repository tree: the runner's worktree containment
    guard (#4444) refuses write-capable spawns from the protected primary
    checkout, and any in-tree path classifies against it. Out-of-tree cwds
    are isolated by definition and skip the git classify entirely.

    #7164: routed to disk-backed fleet scratch root so asks do not exhaust
    tmpfs /tmp quotas, and scanned by tmp leak sweep.
    """
    scratch = ensure_scratch_root() / "learn-ukrainian-bridge-asks" / "agy"
    scratch.mkdir(parents=True, exist_ok=True)
    # R38: an existing scratch hooks file still gets a missing primary-write guard.
    # provision_workspace_guard returns when both guards are present and does not
    # overwrite a key that is already there, including one the user disabled.
    provision_workspace_guard(scratch)
    return scratch


def _resolve_agy_bridge_timeout(no_timeout: bool = False) -> int:
    """Resolve Agy hard timeout from CLI flag/env with a safe fallback.

    ``agent_runtime.runner.invoke()`` requires an integer hard timeout, so
    "no timeout" mode maps to a 24h ceiling rather than ``None``.
    """
    if no_timeout:
        return _NO_TIMEOUT_AGY_BRIDGE_TIMEOUT_SECONDS

    raw = os.environ.get("AGY_BRIDGE_TIMEOUT")
    if raw is None:
        return _DEFAULT_AGY_BRIDGE_TIMEOUT_SECONDS

    value = raw.strip().lower()
    if value in {"0", "none", "off", "false", "no"}:
        return _NO_TIMEOUT_AGY_BRIDGE_TIMEOUT_SECONDS

    try:
        timeout = int(value)
    except ValueError:
        print(f"⚠️  Invalid AGY_BRIDGE_TIMEOUT={raw!r} — falling back to {_DEFAULT_AGY_BRIDGE_TIMEOUT_SECONDS}s")
        return _DEFAULT_AGY_BRIDGE_TIMEOUT_SECONDS

    if timeout <= 0:
        return _NO_TIMEOUT_AGY_BRIDGE_TIMEOUT_SECONDS
    return timeout


# Sealed formal CF isolation is not proven for AGY (project instructions, MCP,
# hooks, nested reviewers cannot yet be suppressed), and the sealed review-pr
# path itself is retired (operator 2026-08-07). Fail closed *before* any send
# on this legacy entrypoint so operators get the working path immediately.
AGY_SEALED_REVIEW_UNSUPPORTED = (
    "agy_isolated_review_unsupported: AGY cannot yet prove native "
    "project-instruction, MCP, hook, and nested-reviewer suppression for "
    "sealed formal CF review, and sealed review-pr is retired. "
    "Use the lightweight direct review on an eligible lane: "
    "`ask-claude|ask-codex - --task-id review-<N> --type review` "
    "(or `--pr <N>`, which routes to the same direct path). "
    "AGY remains fine for advisory ask-agy *without* --review."
)

def ask_agy(
    content: str,
    task_id: str | None = None,
    msg_type: str = "query",
    data: str | None = None,
    new_session: bool = False,
    from_llm: str = "claude",
    from_model: str | None = None,
    to_model: str | None = None,
    no_timeout: bool = False,
    effort: str | None = None,
    review: bool = False,
    stdout_only: bool = False,
    output_path: str | None = None,
    background: bool = False,
    review_branch: str | None = None,
    review_pr_number: int | None = None,
):
    """Send message to Agy AND invoke Agy to process it (one-shot)."""
    require_core_or_exit("ask-agy")
    if background and (stdout_only or output_path):
        raise ValueError("ask-agy --background cannot be combined with --stdout-only or --output-path")
    if review or review_branch is not None or review_pr_number is not None:
        # Refuse formal/exact-target review on AGY before any worktree or send.
        raise ValueError(AGY_SEALED_REVIEW_UNSUPPORTED)
    effective_model = resolve_model_selection(
        lane="ask-agy", to_model=to_model, model=None, default=_default_agy_model()
    )
    msg_id = send_message(
        content,
        task_id,
        msg_type,
        data,
        from_llm=from_llm,
        to_llm="agy",
        from_model=from_model,
        to_model=effective_model,
        effort=effort,
    )
    register_ask(msg_id)
    if background:
        launch_background_ask(
            msg_id,
            "agy",
            {
                "new_session": new_session,
                "no_timeout": no_timeout,
                "review": review,
                "timeout_seconds": 1800,
            },
        )
        return msg_id
    if not stdout_only:
        print(f"\n🚀 Invoking Agy to process message #{msg_id}...")
    response = process_for_agy(
        msg_id,
        new_session,
        no_timeout,
        review=review,
        stdout_only=stdout_only,
        output_path=output_path,
    )
    if stdout_only and response:
        print(response)
    return msg_id


def process_for_agy(
    message_id: int,
    new_session: bool = False,
    no_timeout: bool = False,
    review: bool = False,
    stdout_only: bool = False,
    output_path: str | None = None,
) -> str | None:
    """Read message addressed to Agy, invoke via agent_runtime, send response.

    ``new_session`` is accepted for API parity with the codex bridge but
    is currently a no-op for Agy — the runtime starts a fresh ``agy -p``
    process per invocation. A future revision could pass
    ``--conversation=<session_id>`` to resume a prior turn; today the
    bridge is single-shot.
    """
    from ._messaging import require_legacy_message_writes

    if review:
        raise ValueError(AGY_SEALED_REVIEW_UNSUPPORTED)
    require_legacy_message_writes()
    msg = _fetch_agy_message(message_id)
    if not msg:
        return

    _ = new_session  # No-op for now; Agy bridge calls are always fresh.
    timeout_val = _resolve_agy_bridge_timeout(no_timeout)
    model = _extract_target_model(msg) or _default_agy_model()
    effort = requested_effort(msg)
    effort_applied, effort_reason = unsupported_effort_note(
        lane="agy",
        effort=effort,
        reason="the Antigravity CLI has no per-invocation effort control",
    )

    if not stdout_only:
        print(f"📨 Message #{msg['id']}")
        print(f"   From: {msg['from']} → To: {msg['to']}")
        print(f"   Type: {msg['type']}")
        print(f"   Task: {msg['task_id'] or 'N/A'}")
        print(f"   Model: {model}")
        if timeout_val == _NO_TIMEOUT_AGY_BRIDGE_TIMEOUT_SECONDS:
            print("   Hard timeout: no-timeout requested (24h ceiling)")
        else:
            print(f"   Hard timeout: {timeout_val}s")

    prompt = build_agy_prompt(msg)
    # Bridge Q&A keeps an out-of-tree scratch cwd for write-mode containment
    # and grants repository read access through repo_read_root (--add-dir).
    tool_config = {
        "bridge_repo_read": True,
        "repo_read_root": str(_config.REPO_ROOT),
    }
    try:
        result = agent_runner.invoke(
            "agy",
            prompt,
            # Headless tool prompts need the skip-permissions path.
            mode="danger",
            cwd=_agy_ask_scratch_cwd(),
            model=model,
            effort=effort,
            task_id=msg["task_id"],
            session_id=None,
            tool_config=tool_config,
            entrypoint="bridge",
            hard_timeout=timeout_val,
            stall_timeout=min(600, timeout_val),
        )
    except RateLimitedError as exc:
        _handle_agy_error(msg, message_id, f"Agy rate limited: {exc}")
        return None
    except AgentStalledError as exc:
        _handle_agy_error(msg, message_id, f"Agy stalled: {exc}")
        return None
    except AgentTimeoutError as exc:
        _handle_agy_error(msg, message_id, f"Agy hard timeout: {exc}")
        return None
    except AgentUnavailableError as exc:
        _handle_agy_error(msg, message_id, f"Agy unavailable: {exc}")
        return None

    if not result.ok:
        _handle_agy_error(
            msg,
            message_id,
            result.stderr_excerpt or "Agy returned no final message",
        )
        return None

    if result.session_id and msg["task_id"]:
        set_session(msg["task_id"], "agy", result.session_id)

    response = result.response
    if not response:
        _handle_agy_error(msg, message_id, "Agy returned no final message")
        return None

    if output_path:
        from pathlib import Path

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(response, encoding="utf-8")

    if not stdout_only:
        print(f"\n✅ Agy finished ({len(response)} chars)")
    from lu_machinery.agent_runtime.adapters.agy import agy_model_state

    identity_state = agy_model_state(
        agent="agy", model=model, identity=result.model_identity,
        task_id=msg["task_id"], run_nonce=tool_config.get("run_nonce"),
    )
    provenance_data, actual_model = response_provenance(
        msg,
        actual_model=identity_state["resolved_model"] if identity_state else getattr(result, "model", None) or model,
        harness="agy",
        effort_applied=effort_applied,
        effort_reason=effort_reason,
    )
    if identity_state:
        provenance = json_depth.loads(provenance_data, max_depth=512)
        provenance.update(
            actual_model=identity_state["resolved_model"] if identity_state["resolved_model_known"] else None,
            actual_model_known=identity_state["resolved_model_known"],
            actual_family=identity_state["resolved_model_family"],
            model_identity=identity_state["model_identity"],
        )
        provenance_data = json.dumps(provenance, sort_keys=True)
    reply_id = send_message(
        content=response,
        task_id=msg["task_id"],
        msg_type="response",
        from_llm="agy",
        to_llm=msg["from"],
        data=provenance_data,
        from_model=actual_model,
    )
    acknowledge(message_id)
    record_ask_reply(message_id, reply_id)
    return response


def _fetch_agy_message(message_id: int) -> dict | None:
    """Fetch a message addressed to Agy from the database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, task_id, from_llm, to_llm, message_type, content, data, timestamp
        FROM messages
        WHERE id = ? AND to_llm = 'agy'
        """,
        (message_id,),
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        print(f"❌ Message {message_id} not found or not addressed to Agy")
        return None

    return {
        "id": row[0],
        "task_id": row[1],
        "from": row[2],
        "to": row[3],
        "type": row[4],
        "content": row[5],
        "data": row[6],
        "timestamp": row[7],
    }


def _extract_target_model(msg: dict) -> str | None:
    """Read optional ``to_model`` from the message's ``data`` JSON blob.

    ``send_message`` serializes ``--to-model`` into the ``data`` JSON column
    (NOT a dedicated ``to_model`` column — that column never existed in the
    ``messages`` schema, which is why the old PRAGMA-based lookup always
    returned None and every invocation silently fell back to flash). The
    adapter's ``_resolve_model_flag`` maps the returned slug
    (e.g. ``gemini-3.1-pro-high``) or display label to agy's ``--model``.
    Ported from kubedojo's fixed bridge. Returns None when absent.
    """
    data = msg.get("data")
    if not data:
        return None
    try:
        payload = json_depth.loads(data, max_depth=512)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("to_model")
    return str(model) if model else None


def _handle_agy_error(msg: dict, message_id: int, reason: str) -> None:
    """Record an Agy failure as a response message; no ack (#6915).

    The inbound message stays unacknowledged so the failure is retryable;
    acknowledgement happens only after a successful routed reply.
    """
    print(f"\n❌ Agy error for message #{message_id}: {reason}")
    from ._ask_contract import failed_response_provenance

    data, from_model = failed_response_provenance(msg, bridge_model="agy-bridge-error", harness="agy")
    from lu_machinery.agent_runtime.adapters.agy import agy_model_state

    identity_state = agy_model_state(
        agent="agy", model=_extract_target_model(msg) or _default_agy_model(),
        identity=None, task_id=msg["task_id"], run_nonce=None,
    )
    if identity_state:
        provenance = json_depth.loads(data, max_depth=512)
        provenance.update(from_model="unattested-harness", actual_model=None,
                          actual_model_known=False, actual_family=None, model_identity=None)
        data, from_model = json.dumps(provenance, sort_keys=True), "unattested-harness"
    send_message(
        content=f"[Agy error] {reason}",
        task_id=msg["task_id"],
        msg_type="error",
        from_llm="agy",
        to_llm=msg["from"],
        data=data,
        from_model=from_model,
    )
    record_ask_failure(
        message_id,
        reason,
        timed_out="timeout" in reason.lower() or "stalled" in reason.lower(),
    )
