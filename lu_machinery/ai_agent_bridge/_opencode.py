"""Retained OpenCode transport and Fleet-backed request lifecycle.

OpenCode carries explicit provider/model routes, including OpenRouter, Gemma
and DeepSeek. Public ask helpers persist one request job, claim it with a
fenced lease, and store a terminal result. Historical broker asks are never
claimed by this worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from lu_machinery.common import json_depth
from lu_machinery.common.jsonl import jsonl_lines as split_jsonl_lines
from lu_machinery.common.repo_root import project_interpreter, state_root
from lu_machinery.contracts import new_id
from lu_machinery.fleet_comms.artifacts import ArtifactStore
from lu_machinery.fleet_comms.authority import AuthorityService, AuthorityServiceError
from lu_machinery.fleet_comms.paths import default_plane_root
from lu_machinery.lib.readonly_sqlite import open_readonly
from lu_machinery.orchestration.dispatch_isolation import DispatchIsolationError, spawn_detached_worker
from lu_machinery.review.model_catalog import activity_role_refusal, load_model_catalog, resolve_catalog_model_id
from lu_machinery.secret_redactor import redact_text, redact_value

from ._ask_contract import (
    resolve_model_selection,
    response_provenance,
)
from ._prompts import require_core_or_exit, with_core_or_exit
from ._review_safety import (
    ReviewSafetyError,
    assert_formal_review_ask_payload,
    is_formal_review_ask,
    warn_missing_review_target,
)

# Compatibility defaults; callers may select an explicit supported model.
OPENCODE_DEFAULT_MODEL = "google-ais/gemma-4-31b-it"
OPENCODE_DEFAULT_TIMEOUT_S = 900

# Historical named helpers remain subject to catalog admission.
POOL_MODEL = "poolside/poolside/laguna-s-2.1"
POOL_MODEL_S = POOL_MODEL  # alias: Laguna S 2.1
POOL_MODEL_XS = "poolside/poolside/laguna-xs-2.1"
POOL_MODEL_M1_LEGACY = "poolside/poolside/laguna-m.1"
POOL_DEFAULT_VARIANT = "high"  # reasoning effort: minimal | high | max
POOL_DEFAULT_TIMEOUT_S = 1800  # browsing + high-effort reasoning runs long
POOL_VARIANTS = frozenset({"minimal", "high", "max"})
_EFFORT_TO_VARIANT = {
    "low": "minimal",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}

GLM_MODEL = "zai-coding-plan/glm-5.3"
GLM_DEFAULT_VARIANT = "high"  # default effort when --effort omitted (operator 2026-08-14)
GLM_ADVISORY_VARIANT = "max"  # advisory / hard escalation effort for GLM-5.3
GLM_DEFAULT_TIMEOUT_S = 1800
# Env vars whose presence indicates an automated/CI context where the
# China-egress constraint forbids invoking GLM.
_CI_ENV_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL")

# Max output token budget for opencode-routed reasoning-heavy lanes (glm/pool)
# to give headroom for hidden reasoning before a `step_finish` with reason="length".
# Observed death: ~32k reasoning + 5 output on glm-5.3 (input ~31k). Target:
# reasoning + final output headroom >= 64K (or the provider-advertised max).
#
# Control:
# - Sane default per model (131072 for glm-5.3 which advertises ~131K output).
# - Per-model env overrides: AB_GLM_MAX_OUTPUT_TOKENS, AB_POOL_MAX_OUTPUT_TOKENS
# - Global: AB_OPENCODE_MAX_OUTPUT_TOKENS
# - Falls back to setting OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX for the
#   subprocess (the known lever to lift opencode's internal ~32k cap on some
#   paths / custom providers; see anomalyco/opencode#29363 and related).
# If the provider plan or opencode build still caps it server-side, the
# experimental env is still passed (no placebo; callers can observe via
# raw NDJSON step_finish + tokens).
#
# NOTE: this is *output* (incl. reasoning for these models). Context is separate.
GLM_DEFAULT_MAX_OUTPUT_TOKENS = 131072
POOL_DEFAULT_MAX_OUTPUT_TOKENS = 131072
AB_OPENCODE_MAX_OUTPUT_TOKENS_ENV = "AB_OPENCODE_MAX_OUTPUT_TOKENS"
AB_GLM_MAX_OUTPUT_TOKENS_ENV = "AB_GLM_MAX_OUTPUT_TOKENS"
AB_POOL_MAX_OUTPUT_TOKENS_ENV = "AB_POOL_MAX_OUTPUT_TOKENS"

# Retained hramatka model, used toolless through OpenCode.
GEMMA_MODEL = "google-ais/gemma-4-31b-it"
GEMMA_DEFAULT_TIMEOUT_S = 900  # chat model (no browsing); MoE variants can be slow


def _resolve_opencode_effort(
    *, lane: str, effort: str | None, variant: str | None = None
) -> tuple[str | None, str | None]:
    """Map the uniform contract to opencode's three native variants visibly."""
    requested_variant = (variant or "").strip().lower() or None
    if requested_variant and requested_variant not in POOL_VARIANTS:
        raise SystemExit(f"{lane}: invalid --variant {variant!r} (choose one of {sorted(POOL_VARIANTS)})")
    if not effort:
        return requested_variant, None
    applied = _EFFORT_TO_VARIANT[effort]
    if requested_variant and requested_variant != applied:
        raise ValueError(
            f"{lane}: --effort {effort} maps to opencode --variant {applied}, "
            f"which conflicts with --variant {requested_variant}"
        )
    reason = None
    if applied != effort:
        reason = f"opencode exposes variants minimal/high/max; requested {effort} maps to {applied}"
        print(f"NOTE: {lane} {reason}")
    return applied, reason


STANDING_TOOLLESS_NOTICE = "answer from the attached content; shell/tools are unavailable in this mode"


def _ensure_toolless_prompt_notice(prompt: str) -> str:
    if STANDING_TOOLLESS_NOTICE in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n[{STANDING_TOOLLESS_NOTICE}]"


@dataclass(frozen=True, slots=True)
class OpencodeTurnStatus:
    outcome: str  # "completed" | "aborted" | "permission_rejected" | "errored" | "trace_unavailable"
    reason: str
    cancellation_category: str | None = None
    session_id: str | None = None


class OpencodeTurnError(RuntimeError):
    """Raised when an opencode turn is not completed cleanly."""

    def __init__(self, status: OpencodeTurnStatus, partial_text: str):
        super().__init__(f"opencode turn aborted: {status.outcome}/{status.reason}")
        self.status = status
        self.partial_text = partial_text


def read_opencode_turn_status(
    stdout: str,
    *,
    cwd: Path | None = None,
    opencode_db_path: Path | None = None,
) -> OpencodeTurnStatus:
    """Classify an opencode turn: completed vs aborted/permission-rejected/errored.

    Reads opencode's session/trace state from:
      1. NDJSON stdout stream (events/parts/errors)
      2. opencode.db SQLite database (if present)

    Exit code 0 is PROVEN meaningless — turn state must be verified empirically.
    If session trace state is missing or unreadable and stream has no trace proof,
    fails closed as trace_unavailable.
    """
    session_id: str | None = None
    stream_has_error = False
    stream_error_name: str | None = None
    stream_error_msg: str | None = None
    stream_stop = False
    stream_finish_reason: str | None = None
    permission_cancelled = False
    tool_error_detail: str | None = None
    has_text = False

    raw_stdout = (stdout or "").strip()
    if raw_stdout and not raw_stdout.startswith("{"):
        has_text = True

    for raw_line in split_jsonl_lines(raw_stdout):
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json_depth.loads(line, max_depth=512)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        if (
            event.get("type") is None
            and not isinstance(event.get("part"), dict)
            and not (event.get("sessionID") or event.get("session_id"))
            and event.get("error") is None
        ):
            # A JSON object carrying none of the opencode event signature keys
            # (type/part/session/error) is model CONTENT — e.g. an ask that
            # replies with a bare JSON verdict — not a trace event. Without
            # this, a legitimate pure-JSON reply classifies as
            # trace_unavailable and a good ask is killed (REACH regression).
            has_text = True
            continue

        sid = event.get("sessionID") or event.get("session_id")
        if isinstance(sid, str) and sid:
            session_id = sid
        part = event.get("part")
        if isinstance(part, dict):
            sid_part = part.get("sessionID") or part.get("session_id")
            if isinstance(sid_part, str) and sid_part:
                session_id = sid_part

        event_type = event.get("type")
        if event_type == "text":
            has_text = True
            part_obj = event.get("part")
            if isinstance(part_obj, dict) and isinstance(part_obj.get("text"), str):
                has_text = True
        elif isinstance(part, dict) and part.get("type") == "text":
            has_text = True

        # `error: null` is a benign key on normal events — only a NON-NULL
        # error payload marks the turn errored (a null here previously blocked
        # the has_text completion fallback and killed good turns).
        if event.get("type") == "error" or event.get("error") is not None:
            stream_has_error = True
            err = event.get("error")
            if isinstance(err, dict):
                stream_error_name = str(err.get("name") or err.get("type") or "error")
                err_data = err.get("data")
                if isinstance(err_data, dict):
                    stream_error_msg = str(err_data.get("message") or "")
                elif isinstance(err_data, str):
                    stream_error_msg = err_data
            elif isinstance(err, str):
                stream_error_name = err

        if event_type in ("step_finish", "step-finish") or (
            isinstance(part, dict) and part.get("type") in ("step_finish", "step-finish")
        ):
            reason = (part.get("reason") if isinstance(part, dict) else None) or event.get("reason")
            if isinstance(reason, str):
                stream_finish_reason = reason
                if reason in ("stop", "completed"):
                    stream_stop = True
                elif reason in ("abort", "cancelled", "permission_denied", "permission_rejected", "error", "length"):
                    stream_has_error = True

        if event_type in ("tool", "tool_use") or (isinstance(part, dict) and part.get("type") in ("tool", "tool_use")):
            st = None
            if isinstance(part, dict):
                state = part.get("state")
                st = state.get("status") if isinstance(state, dict) else part.get("status")
            if st is None:
                st = event.get("status")

            if isinstance(st, str) and st in ("rejected", "cancelled", "permission_denied", "permission_rejected"):
                permission_cancelled = True
                tool = part.get("tool") if isinstance(part, dict) else None
                tool_error_detail = f"tool call {tool or 'unknown'} permission cancelled ({st})"

    db_path = opencode_db_path
    if db_path is None:
        raw_db_env = os.environ.get("OPENCODE_DB_PATH")
        if raw_db_env:
            db_path = Path(raw_db_env)
        else:
            data_home = Path(
                os.environ.get("OPENCODE_HOME")
                or os.environ.get("XDG_DATA_HOME")
                or (Path.home() / ".local" / "share")
            )
            db_path = data_home / "opencode" / "opencode.db"

    db_error_name: str | None = None
    db_error_msg: str | None = None
    db_finish_reason: str | None = None
    db_permission_cancelled = False

    if db_path and db_path.exists():
        try:

            conn = open_readonly(db_path)
            try:
                cur = conn.cursor()
                target_sid = session_id
                if not target_sid and cwd is not None:
                    cur.execute(
                        "SELECT id FROM session WHERE directory = ? ORDER BY time_updated DESC LIMIT 1",
                        (str(cwd.resolve()),),
                    )
                    row = cur.fetchone()
                    if row:
                        target_sid = row[0]

                if target_sid:
                    session_id = target_sid
                    cur.execute(
                        "SELECT data FROM message WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant' ORDER BY time_created DESC LIMIT 1",
                        (target_sid,),
                    )
                    msg_row = cur.fetchone()
                    if msg_row and msg_row[0]:
                        try:
                            msg_data = json_depth.loads(msg_row[0], max_depth=512)
                            if isinstance(msg_data, dict):
                                err = msg_data.get("error")
                                if isinstance(err, dict):
                                    db_error_name = str(err.get("name") or "error")
                                    err_d = err.get("data")
                                    if isinstance(err_d, dict):
                                        db_error_msg = str(err_d.get("message") or "")
                                db_finish_reason = msg_data.get("finish")
                        except json.JSONDecodeError:
                            pass

                    cur.execute(
                        "SELECT data FROM part WHERE session_id = ? ORDER BY time_created DESC",
                        (target_sid,),
                    )
                    for part_row in cur.fetchall():
                        if not part_row or not part_row[0]:
                            continue
                        try:
                            pdata = json_depth.loads(part_row[0], max_depth=512)
                            if isinstance(pdata, dict):
                                ptype = pdata.get("type")
                                if ptype in ("step-finish", "step_finish"):
                                    r = pdata.get("reason")
                                    if isinstance(r, str) and not db_finish_reason:
                                        db_finish_reason = r
                                elif ptype in ("tool", "tool_use"):
                                    st = (
                                        pdata.get("state", {}).get("status")
                                        if isinstance(pdata.get("state"), dict)
                                        else pdata.get("status")
                                    )
                                    if isinstance(st, str) and st in (
                                        "rejected",
                                        "cancelled",
                                        "permission_denied",
                                        "permission_rejected",
                                    ):
                                        db_permission_cancelled = True
                        except json.JSONDecodeError:
                            pass
            finally:
                conn.close()
        except Exception:
            pass

    if permission_cancelled or db_permission_cancelled:
        return OpencodeTurnStatus(
            outcome="permission_rejected",
            reason=tool_error_detail or "tool call permission cancelled by policy",
            cancellation_category="permission_rejected",
            session_id=session_id,
        )

    err_name = db_error_name or stream_error_name
    err_msg = db_error_msg or stream_error_msg
    if err_name:
        err_lower = (err_name + " " + (err_msg or "")).lower()
        if "permission" in err_lower or "denied" in err_lower:
            return OpencodeTurnStatus(
                outcome="permission_rejected",
                reason=err_msg or err_name,
                cancellation_category="permission_rejected",
                session_id=session_id,
            )
        if "abort" in err_lower or "cancel" in err_lower:
            return OpencodeTurnStatus(
                outcome="aborted",
                reason=err_msg or err_name,
                cancellation_category="aborted",
                session_id=session_id,
            )
        return OpencodeTurnStatus(
            outcome="errored",
            reason=err_msg or err_name,
            cancellation_category="error",
            session_id=session_id,
        )

    finish_reason = db_finish_reason or stream_finish_reason
    if finish_reason:
        if finish_reason in ("stop", "completed"):
            return OpencodeTurnStatus(
                outcome="completed",
                reason="stop",
                session_id=session_id,
            )
        if finish_reason in ("abort", "cancelled"):
            return OpencodeTurnStatus(
                outcome="aborted",
                reason=f"finish reason {finish_reason}",
                cancellation_category="aborted",
                session_id=session_id,
            )
        if finish_reason in ("permission_denied", "permission_rejected"):
            return OpencodeTurnStatus(
                outcome="permission_rejected",
                reason=f"finish reason {finish_reason}",
                cancellation_category="permission_rejected",
                session_id=session_id,
            )
        if finish_reason in ("length", "error", "unknown"):
            cat = "length" if finish_reason == "length" else "error"
            return OpencodeTurnStatus(
                outcome="errored",
                reason=f"finish reason {finish_reason}",
                cancellation_category=cat,
                session_id=session_id,
            )

    if not stream_has_error and (stream_stop or has_text):
        return OpencodeTurnStatus(
            outcome="completed",
            reason="stop",
            session_id=session_id,
        )

    return OpencodeTurnStatus(
        outcome="trace_unavailable",
        reason="missing or unreadable session state",
        cancellation_category="trace_unavailable",
        session_id=session_id,
    )


def read_opencode_result(job_id: str) -> dict:
    """Read the durable result without initializing storage or invoking a provider."""
    with ArtifactStore(readonly=True) as store, AuthorityService(store=store) as authority:
        result = authority.read_job_result(job_id)
        if result is None:
            raise AuthorityServiceError("opencode_terminal_result_missing")
        payload = json.loads(result)
        if not isinstance(payload, dict):
            raise AuthorityServiceError("opencode_terminal_result_invalid")
        return payload


def print_authority_asks(task_id: str | None = None) -> None:
    """List native request jobs, including queued and failed OpenCode asks."""
    with ArtifactStore(readonly=True) as store, AuthorityService(store=store):
        rows = store.connection.execute(
            "SELECT j.job_id, j.state, j.created_at, j.payload_artifact_id "
            "FROM authority_jobs j JOIN authority_message_metadata meta ON meta.message_id=j.subject_id "
            "WHERE j.job_kind='request' AND (? IS NULL OR json_extract(meta.provenance_json, '$.task_id')=?) "
            "ORDER BY j.created_at DESC, j.job_id DESC LIMIT 100", (task_id, task_id),
        ).fetchall()
        print("ID  TASK  TO  STATUS  SENT" if rows else "No tracked asks.")
        for row in rows:
            payload = json.loads(store.read_bytes(row["payload_artifact_id"]))
            metadata = payload["metadata"]
            print(f"{row['job_id']}  {metadata.get('task_id', '-')}  {payload['recipient']}  {row['state']}  {row['created_at']}")


def _enqueue_opencode_ask(
    content: str, task_id: str, target: str, *, model: str, variant: str | None,
    effort: str | None, effort_reason: str | None, data: str | None,
    msg_type: str, from_llm: str, from_model: str | None,
    no_timeout: bool, background: bool,
    on_message_created: Callable[[str], None] | None = None,
) -> str:
    """Use one Fleet request job as the durable OpenCode lifecycle.

    The job ID replaces the broker's integer message ID. Repeating the same
    task and inputs replays its result; a running lease cannot be stolen.
    Background workers re-enter through process-ask and read only the job.
    """
    activity = "review" if is_formal_review_ask(msg_type=msg_type, task_id=task_id) else (
        "consult" if msg_type.strip().lower() in {"advisory", "consult", "discussion"} else None
    )
    if activity:
        catalog = load_model_catalog()
        if resolve_catalog_model_id(model, catalog) is None:
            raise ValueError(f"ask-{target}: model is not catalogued for {activity}")
        if refusal := activity_role_refusal(model, activity, catalog):
            raise ValueError(f"ask-{target}: {refusal}")
    content = redact_text(content) or ""
    attachment = Path(data).resolve() if data else None
    metadata = {
        "transport": "opencode", "target": target, "task_id": task_id,
        "requested_model": model, "from_model": from_model, "effort": effort,
        "variant": variant, "effort_reason": effort_reason,
        "data": str(attachment) if attachment is not None else None,
        "msg_type": msg_type, "no_timeout": no_timeout,
    }
    metadata = redact_value(metadata)
    # Generated integrity data is trusted, not caller text for secret redaction.
    metadata["attachment_sha256"] = hashlib.sha256(attachment.read_bytes()).hexdigest() if attachment is not None else None
    digest = hashlib.sha256(json.dumps(
        {"sender": from_llm, "body": content, "metadata": metadata}, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    with AuthorityService() as authority:
        job = authority.enqueue_request(
            recipient=target, sender=from_llm, body=content,
            metadata=metadata, idempotency_key=f"opencode-ask:{digest}",
        )
    if on_message_created is not None:
        on_message_created(job.job_id)
    if background and job.state == "queued":
        log_dir = default_plane_root() / "opencode-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (log_dir / f"{job.job_id}.log").open("a", encoding="utf-8") as log:
                # This allowlisted argv is never shell text or provider output.
                command = [
                    str(project_interpreter()), "-I", "-u", "-m", "lu_machinery.ai_agent_bridge",
                    "process-ask", job.job_id, target,
                ]
                spawn_detached_worker(
                    command, task_id=job.job_id, run_nonce=new_id("opencode-launch"),
                    popen=partial(subprocess.Popen, cwd=str(state_root())),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    stderr_log=log_dir / f"{job.job_id}.log", env=os.environ.copy(),
                )
        except (OSError, DispatchIsolationError) as exc:
            # Leave the durable request queued for explicit process-ask recovery.
            raise AuthorityServiceError("opencode_worker_launch_failed_job_queued") from exc
        print(f"OpenCode job queued: {job.job_id}")
    elif not background:
        process_for_opencode(job.job_id, target=target)
    else:
        print(f"OpenCode job {job.job_id}: {job.state}")
    return job.job_id


def ask_opencode(
    content: str,
    task_id: str,
    msg_type: str = "query",
    data: str | None = None,
    model: str | None = None,
    from_llm: str = "claude",
    from_model: str | None = None,
    to_model: str | None = None,
    effort: str | None = None,
    no_timeout: bool = False,
    background: bool = False,
) -> str:
    """Queue an explicit OpenCode provider/model request through Fleet Comms."""
    require_core_or_exit("ask-opencode")
    effective_model = resolve_model_selection(
        lane="ask-opencode", to_model=to_model, model=model, default=OPENCODE_DEFAULT_MODEL
    )
    effective_variant, effort_reason = _resolve_opencode_effort(lane="ask-opencode", effort=effort)
    return _enqueue_opencode_ask(
        content, task_id, 'opencode', model=effective_model, variant=effective_variant,
        effort=effort, effort_reason=effort_reason, data=data, msg_type=msg_type,
        from_llm=from_llm, from_model=from_model, no_timeout=no_timeout,
        background=background, on_message_created=None,
    )

def ask_pool(
    content: str,
    task_id: str,
    msg_type: str = "query",
    data: str | None = None,
    variant: str | None = None,
    model: str | None = None,
    to_model: str | None = None,
    effort: str | None = None,
    from_llm: str = "claude",
    from_model: str | None = None,
    no_timeout: bool = False,
    background: bool = False,
    on_message_created: Callable[[str], None] | None = None,
    review_pr_lifecycle: bool = False,
) -> str:
    """Retain the historical named helper, subject to catalog admission."""
    require_core_or_exit("ask-pool")
    effective_variant, effort_reason = _resolve_opencode_effort(
        lane="ask-pool", effort=effort, variant=variant
    )
    effective_variant = effective_variant or POOL_DEFAULT_VARIANT
    effective_model = resolve_model_selection(
        lane="ask-pool", to_model=to_model, model=model, default=POOL_MODEL
    )
    return _enqueue_opencode_ask(
        content, task_id, 'pool', model=effective_model, variant=effective_variant,
        effort=effort, effort_reason=effort_reason, data=data, msg_type=msg_type,
        from_llm=from_llm, from_model=from_model, no_timeout=no_timeout,
        background=background, on_message_created=on_message_created,
    )

def _assert_glm_egress_allowed(verb: str = "ask-glm") -> None:
    """Refuse to run China-hosted GLM in a CI / automated context (data egress).

    GLM (Zhipu/z.ai) sends prompt content to China. The bridge is a local,
    interactive tool; this backstop makes the LOCAL-ONLY policy load-bearing so
    a future pipeline change can't silently egress data to a China-hosted model.
    """
    for var in _CI_ENV_VARS:
        # Presence check, not truthiness: a set-but-empty var (CI="") must STILL
        # refuse — for a China-egress guard, err on the side of not sending data.
        if var in os.environ:
            raise SystemExit(
                f"{verb}: refusing to run under {var}. GLM is "
                "China-hosted (Zhipu/z.ai) → prompt data egresses to China; it "
                "is LOCAL-ONLY and must never run in CI / automated pipelines."
            )


def ask_glm(
    content: str,
    task_id: str,
    msg_type: str = "query",
    data: str | None = None,
    model: str | None = None,
    to_model: str | None = None,
    effort: str | None = None,
    from_llm: str = "claude",
    from_model: str | None = None,
    no_timeout: bool = False,
    background: bool = False,
    on_message_created: Callable[[str], None] | None = None,
    review_pr_lifecycle: bool = False,
) -> str:
    """Retain the historical GLM helper and its egress and catalog guards."""
    require_core_or_exit("ask-glm")
    try:
        formal_review = assert_formal_review_ask_payload(
            content,
            msg_type=msg_type,
            task_id=task_id,
            attachment=data,
            has_target=False,
        )
    except ReviewSafetyError as exc:
        raise SystemExit(f"ask-glm: {exc}") from exc
    warn_missing_review_target(formal_review=formal_review, has_target=False)
    _assert_glm_egress_allowed()
    effective_model = resolve_model_selection(
        lane="ask-glm", to_model=to_model, model=model, default=GLM_MODEL
    )
    # Advisory consultations default to max; everyday/query defaults to high.
    # Explicit --effort always wins (operator 2026-08-14 / GLM-5.3).
    if effort is None and str(msg_type or "").strip().lower() == "advisory":
        effort = "max"
    effective_variant, effort_reason = _resolve_opencode_effort(lane="ask-glm", effort=effort)
    effective_variant = effective_variant or GLM_DEFAULT_VARIANT
    return _enqueue_opencode_ask(
        content, task_id, 'glm', model=effective_model, variant=effective_variant,
        effort=effort, effort_reason=effort_reason, data=data, msg_type=msg_type,
        from_llm=from_llm, from_model=from_model, no_timeout=no_timeout,
        background=background, on_message_created=on_message_created,
    )

def ask_gemma(
    content: str,
    task_id: str,
    msg_type: str = "query",
    data: str | None = None,
    model: str | None = None,
    to_model: str | None = None,
    effort: str | None = None,
    from_llm: str = "claude",
    from_model: str | None = None,
    no_timeout: bool = False,
    background: bool = False,
) -> str:
    """Queue a toolless hramatka Gemma request through Fleet Comms.

    An explicit model selects an available OpenCode route; no provider
    fallback changes the requested model or its attribution."""
    require_core_or_exit("ask-gemma")
    effective_model = resolve_model_selection(
        lane="ask-gemma", to_model=to_model, model=model, default=GEMMA_MODEL
    )
    effective_variant, effort_reason = _resolve_opencode_effort(lane="ask-gemma", effort=effort)
    return _enqueue_opencode_ask(
        content, task_id, 'gemma', model=effective_model, variant=effective_variant,
        effort=effort, effort_reason=effort_reason, data=data, msg_type=msg_type,
        from_llm=from_llm, from_model=from_model, no_timeout=no_timeout,
        background=background, on_message_created=None,
    )

def process_for_opencode(
    message_id: str, *, target: str, no_timeout: bool | None = None,
    variant: str | None = None,
) -> None:
    """Claim a Fleet job, invoke once, and persist success or typed failure.

    A terminal job replays without a provider call. A crashed worker's lease
    is reclaimed only by the Fleet job state machine, preserving fencing.
    """
    if not isinstance(message_id, str) or not message_id.startswith("authority-job_"):
        raise AuthorityServiceError("legacy_opencode_ask_not_claimable_use_fleet_job")
    worker_id = new_id("opencode-worker")
    with AuthorityService() as authority:
        job = authority.get_job(message_id)
        payload = json.loads(authority.store.read_bytes(job.payload_artifact_id))
        metadata = payload["metadata"]
        if job.job_kind != "request" or metadata.get("transport") != "opencode" or metadata.get("target") != target:
            raise AuthorityServiceError("opencode_job_target_mismatch")
        if job.state in {"complete", "failed", "expired", "dead_lettered"}:
            raw = authority.read_job_result(job.job_id)
            if raw is None:
                raise AuthorityServiceError("opencode_terminal_result_missing")
            receipt = json.loads(raw)
            if job.state != "complete":
                raise SystemExit(receipt.get("error") or "opencode job failed")
            print(receipt["response"])
            return
        model = metadata["requested_model"]
        if no_timeout is not None and no_timeout != metadata["no_timeout"]:
            raise AuthorityServiceError("opencode_job_timeout_conflict")
        if variant is not None and variant != metadata["variant"]:
            raise AuthorityServiceError("opencode_job_variant_conflict")
        timeout = {
            "opencode": OPENCODE_DEFAULT_TIMEOUT_S, "gemma": GEMMA_DEFAULT_TIMEOUT_S,
            "pool": POOL_DEFAULT_TIMEOUT_S, "glm": GLM_DEFAULT_TIMEOUT_S,
        }.get(target)
        if timeout is None:
            raise AuthorityServiceError("unsupported_opencode_job_target")
        lease = authority.claim_job(
            job.job_id, worker_id, lease_seconds=86400 if metadata["no_timeout"] else timeout + 30,
        )
        msg = {
            "task_id": metadata["task_id"], "from": authority.get_message(job.subject_id).sender,
            "data": json.dumps({"to_model": model, "effort": metadata["effort"]}),
        }
        provenance, actual_model = response_provenance(
            msg, actual_model=model, harness="opencode", effort_applied=metadata["variant"],
            effort_reason=metadata["effort_reason"],
        )
        receipt = {
            "job_id": job.job_id, "request_message_id": job.subject_id,
            "task_id": metadata["task_id"], "from": target, "to": msg["from"],
            "from_model": actual_model, "to_model": metadata["from_model"],
            "provenance": json.loads(provenance),
        }
        try:
            content = authority.read_message_body(job.subject_id)
            if not content.strip():
                raise AuthorityServiceError("opencode_ask_content_empty")
            attachment = metadata["data"]
            if attachment and hashlib.sha256(Path(attachment).read_bytes()).hexdigest() != metadata["attachment_sha256"]:
                raise AuthorityServiceError("opencode_attachment_changed")
            if target == "glm":
                _assert_glm_egress_allowed("process background ask-glm")
            response = _invoke_opencode(
                content, model, variant=metadata["variant"], data=metadata["data"],
                no_timeout=metadata["no_timeout"], default_timeout_s=timeout,
                output_format="default" if target == "opencode" else "json",
                agent="chat" if target == "gemma" else None,
            )
            response = redact_text(response) or ""
            if target == "gemma":
                response = _strip_gemma_thought(response)
            if not response.strip():
                raise AuthorityServiceError("opencode_response_empty")
        except BaseException as exc:
            receipt.update(state="failed", error=f"OpenCode failed: {type(exc).__name__}")
            if isinstance(exc, OpencodeTurnError):
                receipt.update(
                    error=f"[Bridge Error] opencode turn aborted ({exc.status.reason})",
                    turn_outcome=exc.status.outcome,
                    cancellation_category=exc.status.cancellation_category,
                    partial_response=redact_text(exc.partial_text) or "",
                )
            authority.finish_job(
                job.job_id, worker_id=worker_id, fence_token=lease.fence_token,
                state="failed", result=json.dumps(receipt, sort_keys=True).encode("utf-8"),
                failure={"phase": "provider", "code": "provider_error", "retryable": False},
            )
            if isinstance(exc, OpencodeTurnError):
                raise SystemExit(receipt["error"]) from exc
            raise
        receipt.update(state="complete", response=response)
        authority.finish_job(
            job.job_id, worker_id=worker_id, fence_token=lease.fence_token,
            state="complete", result=json.dumps(receipt, sort_keys=True).encode("utf-8"),
        )
        print(response)


def _strip_gemma_thought(text: str) -> str:
    """Drop a leading Gemma ``<thought>...</thought>`` block, never to empty.

    If the model closed the run inside the thought block (observed live
    2026-07-07), stripping would deliver a blank reply — in that case return
    the original text so the content survives, thought scaffolding and all.
    """
    stripped = re.sub(r"^\s*<thought>.*?</thought>\s*", "", text, flags=re.DOTALL)
    return stripped if stripped.strip() else text


def _get_max_output_tokens(model: str) -> int | None:
    """Return the output token budget (incl. reasoning) to request for this model.

    Resolution (highest to lowest precedence):
      1. model-specific AB_* env (AB_GLM_MAX_OUTPUT_TOKENS etc.)
      2. global AB_OPENCODE_MAX_OUTPUT_TOKENS
      3. per-model sane default (131072 for glm/pool)
      4. None (do not override; let opencode/provider decide)

    The returned value (if any) is injected as OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX
    into the opencode subprocess env. This is the current mechanism to raise
    beyond opencode's ~32k internal default on some reasoning/custom paths.
    """
    # Model-specific envs take precedence
    if model == GLM_MODEL or "glm" in model.lower() or model.startswith("zai"):
        v = os.environ.get(AB_GLM_MAX_OUTPUT_TOKENS_ENV)
        if v and v.strip():
            try:
                return int(v)
            except ValueError:
                pass
    if model == POOL_MODEL or "poolside" in model.lower() or "pool" in model.lower():
        v = os.environ.get(AB_POOL_MAX_OUTPUT_TOKENS_ENV)
        if v and v.strip():
            try:
                return int(v)
            except ValueError:
                pass

    # Global override
    v = os.environ.get(AB_OPENCODE_MAX_OUTPUT_TOKENS_ENV)
    if v and v.strip():
        try:
            return int(v)
        except ValueError:
            pass

    # Sane defaults for the reasoning lanes that exhibited the length death.
    if model == GLM_MODEL or "glm" in model.lower() or model.startswith("zai"):
        return GLM_DEFAULT_MAX_OUTPUT_TOKENS
    if model == POOL_MODEL or "poolside" in model.lower():
        return POOL_DEFAULT_MAX_OUTPUT_TOKENS

    return None


@dataclass(frozen=True, slots=True)
class OpencodeStreamParse:
    """One-pass parse of an opencode ``--format json`` (NDJSON) stream.

    ``text`` is the model's *complete final output*: the last assistant
    message (post any tool-using steps / narration) when the opencode agent
    loop emits multiple assistant turns; falls back to the single/only
    message or raw stdout. This is the value the thin
    :func:`_parse_opencode_ndjson` wrapper returns for ask-*-one-shots.
    We deliberately return the LAST assistant message (not the first
    streamed chunk and not a cross-turn concatenation) because the reply
    of record for a one-shot bridge ask (ask-glm, ask-pool, ...) must be
    the model's terminal substantive answer, not preamble narration such
    as "Let me fetch…". See #5091.

    ``tool_events`` is the deduped, ordered tuple of MCP/tool invocations
    the model made during the run — the per-run observability the
    tool-theatre and grounding gates (#2156) are built on. Each event is
    a minimal ``{tool, input, status, tool_call_id, output}`` dict.
    """

    text: str
    tool_events: tuple[dict, ...]


def _run_opencode(
    content: str,
    model: str,
    *,
    variant: str | None = None,
    output_format: str = "default",
    data: str | None = None,
    no_timeout: bool = False,
    default_timeout_s: int = OPENCODE_DEFAULT_TIMEOUT_S,
    cwd: Path | None = None,
    agent: str | None = None,
) -> str:
    """Run one ``opencode run`` subprocess and return its raw stdout.

    Shared subprocess core for both :func:`_invoke_opencode` (text-only) and
    :func:`_invoke_opencode_detailed` (text + tool telemetry) so the argv
    construction, timeout, and error handling live in exactly one place.

    ``cwd`` sets the subprocess working directory. It defaults to ``None``
    (inherit the parent process cwd) so writer/bridge lanes that legitimately
    edit the repo are unaffected. Read-only lanes (the reviewer transport)
    pass an out-of-repo directory so a tool-using model's stray relative
    writes land outside the checkout (#4642 second leak path).
    """
    from .routing_guard import assert_model_routing_allowed

    assert_model_routing_allowed(model, context="opencode transport (_run_opencode)")
    opencode_bin = shutil.which("opencode")
    if not opencode_bin:
        raise SystemExit("ask-opencode: opencode CLI not found in PATH")

    argv = [opencode_bin, "run", "--model", model, "--format", output_format]
    if agent:
        # Named opencode agent (e.g. "chat" — the TOOLLESS seat). Chat-only
        # models (gemma) must run toolless: the aggregate tool bundle adds
        # ~30K prompt tokens and non-tool-calling models flail in the loop;
        # Google-native upstreams additionally hard-reject one malformed
        # builtin/lightpanda tool schema (2026-07-07 probes).
        argv.extend(["--agent", agent])
        content = _ensure_toolless_prompt_notice(content)
    if variant:
        argv.extend(["--variant", variant])
    if data:
        data_path = Path(data)
        if not data_path.exists():
            raise SystemExit(f"ask-opencode: --data file does not exist: {data}")
        argv.extend(["--file", str(data_path.resolve())])
    # Leave the positional message empty: OpenCode reads non-TTY stdin.
    # --file remains an attachment, never the prepared prompt transport.
    argv.append("--")
    prompt = with_core_or_exit(content, "ask-opencode")

    # Inject per-model (or env-overridable) output token budget for reasoning
    # models. This is passed via the known experimental lever so that glm/pool
    # reasoning does not hit the opencode-internal ~32k cap and die with
    # step_finish reason=length before producing the final message.
    env = os.environ.copy()
    max_tokens = _get_max_output_tokens(model)
    if max_tokens is not None:
        env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = str(max_tokens)

    timeout = None if no_timeout else default_timeout_s
    try:
        result = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"ask-opencode: opencode timed out after {timeout}s") from exc

    if result.returncode != 0:
        raise SystemExit(f"ask-opencode: opencode exited {result.returncode}\nstderr: {result.stderr[-2000:]}")

    return result.stdout


def _invoke_opencode(
    content: str,
    model: str,
    *,
    variant: str | None = None,
    output_format: str = "default",
    data: str | None = None,
    no_timeout: bool = False,
    default_timeout_s: int = OPENCODE_DEFAULT_TIMEOUT_S,
    agent: str | None = None,
    cwd: Path | None = None,
) -> str:
    stdout = _run_opencode(
        content,
        model,
        variant=variant,
        output_format=output_format,
        data=data,
        no_timeout=no_timeout,
        default_timeout_s=default_timeout_s,
        agent=agent,
        cwd=cwd,
    )
    turn_status = read_opencode_turn_status(stdout, cwd=cwd)
    parsed_text = _parse_opencode_ndjson(stdout) if output_format == "json" else stdout.strip()

    if turn_status.outcome != "completed":
        raise OpencodeTurnError(turn_status, parsed_text)

    return parsed_text


def _invoke_opencode_detailed(
    content: str,
    model: str,
    *,
    variant: str | None = None,
    output_format: str = "json",
    data: str | None = None,
    no_timeout: bool = False,
    default_timeout_s: int = OPENCODE_DEFAULT_TIMEOUT_S,
    cwd: Path | None = None,
) -> OpencodeStreamParse:
    """Invoke opencode and return assistant text **plus** tool telemetry.

    Only the audit reviewer-dispatch layer needs this; the bridge verbs keep
    using :func:`_invoke_opencode` (``-> str``). Tool events only exist in the
    NDJSON stream, so this defaults ``output_format="json"``; a ``default``
    format run yields no tool events.

    ``cwd`` is forwarded to :func:`_run_opencode`; the reviewer transport
    passes an out-of-repo directory to firewall stray model writes (#4642).
    """
    stdout = _run_opencode(
        content,
        model,
        variant=variant,
        output_format=output_format,
        data=data,
        no_timeout=no_timeout,
        default_timeout_s=default_timeout_s,
        cwd=cwd,
    )
    turn_status = read_opencode_turn_status(stdout, cwd=cwd)
    parse = (
        _parse_opencode_stream(stdout)
        if output_format == "json"
        else OpencodeStreamParse(text=stdout.strip(), tool_events=())
    )

    if turn_status.outcome != "completed":
        raise OpencodeTurnError(turn_status, parse.text)

    return parse


def _extract_tool_event(event: dict) -> dict | None:
    """Normalize one NDJSON tool event to a compact telemetry dict.

    Handles the observed opencode shape (top-level ``type == "tool_use"`` with a
    nested ``part`` whose ``state`` carries ``input``/``status``/``output`` and
    whose ``callID`` is the tool-call id) while tolerating flatter/older shapes.
    Structured outputs are serialized to JSON text; ``None`` is kept only when
    opencode genuinely emitted no output.
    """
    part = event.get("part")
    if not isinstance(part, dict):
        part = {}
    tool = part.get("tool") or part.get("name") or event.get("tool")
    if not isinstance(tool, str) or not tool:
        return None
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool_input = state.get("input", part.get("input"))
    output = state.get("output", part.get("output"))
    if output is None:
        output_text = None
    elif isinstance(output, str):
        output_text = output
    elif isinstance(output, (dict, list)):
        output_text = json.dumps(output, ensure_ascii=False, default=str)
    else:
        output_text = str(output)
    status = state.get("status") or part.get("status") or event.get("status")
    tool_call_id = part.get("callID") or part.get("tool_call_id") or part.get("id") or event.get("callID")
    return {
        "tool": tool,
        "input": tool_input,
        "status": status if isinstance(status, str) else None,
        "tool_call_id": tool_call_id if isinstance(tool_call_id, str) else None,
        "output": output_text,
    }


def _tool_event_key(event: dict) -> str:
    """Stable dedupe key for a tool event.

    ``tool_call_id`` is the PRIMARY key when present: pending->completed
    transitions of one call share an id (collapse), while distinct repeated
    identical calls keep distinct ids (counted — retry/redundancy signal the
    theatre gate needs; codex review of #4401). Falls back to tool name +
    canonical input JSON when the stream carries no id.
    """
    call_id = event.get("tool_call_id")
    if isinstance(call_id, str) and call_id:
        return f"id\0{call_id}"
    try:
        input_json = json.dumps(event.get("input"), sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        input_json = str(event.get("input"))
    return f"{event.get('tool')}\0{input_json}"


def _parse_opencode_stream(stdout: str) -> OpencodeStreamParse:
    """Parse an opencode NDJSON stream ONCE into text + deduped tool events.

    Assistant text lives in ``type == "text"`` events (``part.text``); tool
    invocations live in ``type in {"tool", "tool_use"}`` events.

    Per the opencode stream contract for agent/tool-using runs (pool/glm
    default agent; ask-glm/ask-pool one-shots), a single ``run`` can emit
    multiple assistant generations: an initial "narration" assistant message
    ("Let me fetch…", "I'll read the design…") followed (after tool results)
    by a subsequent final assistant message containing the substantive
    answer. We return only the *last* assistant message's concatenated text
    parts as ``text`` (the model's complete final output), never the first
    streamed chunk. This is the correct capture layer for bridge one-shot
    replies (process-ask path and direct ask-glm/ask-pool). Cross-turn
    gluing is avoided; single-turn streams are unaffected.

    Tool events are deduped by ``(tool, input-json)`` keeping the FINAL
    status (opencode may emit the same call multiple times as it transitions
    pending -> completed) while preserving first-seen order. Falls back to
    raw (stripped) stdout for text if no text parts parse — robust to
    opencode format drift.
    """
    current_turn: list[str] = []
    assistant_messages: list[str] = []
    deduped: dict[str, dict] = {}
    for raw_line in split_jsonl_lines(stdout):
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json_depth.loads(line, max_depth=512)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "text":
            part = event.get("part") or {}
            text = part.get("text")
            if isinstance(text, str):
                current_turn.append(text)
            continue
        if event_type in ("tool", "tool_use"):
            if current_turn:
                # Commit the just-finished assistant turn (e.g. preamble)
                # before processing the tool that followed it.
                msg = "".join(current_turn).strip()
                if msg:
                    assistant_messages.append(msg)
                current_turn = []
            extracted = _extract_tool_event(event)
            if extracted is not None:
                # Overwrite keeps the FINAL status; dict order keeps first-seen.
                deduped[_tool_event_key(extracted)] = extracted
            continue
        if event_type in ("step_finish", "step_start"):
            if current_turn:
                msg = "".join(current_turn).strip()
                if msg:
                    assistant_messages.append(msg)
                current_turn = []
            continue

    if current_turn:
        msg = "".join(current_turn).strip()
        if msg:
            assistant_messages.append(msg)

    # Reply of record = LAST substantive assistant message (final output),
    # not first streamed chunk. This fixes the one-shot opencode capture
    # for multi-step tasks (ask-glm, ask-pool via process-ask). Refs #5091.
    parsed = next((m for m in reversed(assistant_messages) if m), "") if assistant_messages else ""

    text = parsed if parsed else stdout.strip()
    return OpencodeStreamParse(text=text, tool_events=tuple(deduped.values()))


def _parse_opencode_ndjson(stdout: str) -> str:
    """Extract the model's complete *final* output (last assistant message)
    from opencode ``--format json`` output.

    Thin ``.text`` wrapper over :func:`_parse_opencode_stream` — signature kept
    ``-> str`` because ~8 bridge call sites (ask_gemma/ask_pool/ask_glm/ask-opencode
    and process-ask paths) and their tests assume a plain string.

    The selection of LAST (not first) message is the fix for the capture
    defect in the opencode ask lane. See _parse_opencode_stream and #5091.
    """
    return _parse_opencode_stream(stdout).text
