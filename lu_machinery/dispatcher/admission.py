"""Launch protections the legacy launcher applied: Claude account and systemd placement.

These helpers retain the account and placement contracts used by dispatch v2 (C-2188 unit D).
"""

import contextlib
import functools
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from lu_machinery.dispatcher import providers, store
from lu_machinery.dispatcher.types import StartRefusal
from lu_machinery.orchestration import dispatch_isolation
from lu_machinery.orchestration.task_record_store import REVIEW_TASK_FLAG_FIELDS


@contextlib.contextmanager
def record_refusal(args, agent: str, reason: StartRefusal, *, task_id: str | None = None, hold: dict | None = None):
    """Keep an operational admission refusal while preserving the CLI error and its exit code."""
    try:
        yield
    except (OSError, ValueError, subprocess.SubprocessError) as refusal_exc:
        if agent in providers.PROVIDERS and not (args.dry_run or args.print_advisory_binding):
            admission_record = getattr(refusal_exc, "admission_record", None)
            try:
                store.record_admission_refusal(
                    task_id or args.task_id,
                    agent,
                    vars(args),
                    reason,
                    hold=hold,
                    detail=str(refusal_exc),
                    admission=admission_record if isinstance(admission_record, dict) else None,
                )
            except (OSError, store.TaskRecordError) as exc:
                logging.getLogger(__name__).warning("could not record pre-start refusal: %s", type(exc).__name__)
        raise


def review_typed(record: dict) -> bool:
    """True when a PR target or any review flag types this task as a review (``delegate._dispatch_is_review_typed``).

    v2 has no ``--review``/``--type`` flags; ``--risk`` is stored as ``review_risk`` and a bound
    review id as ``review_attempt``.
    """
    return any(
        record.get(name) not in (None, False, "", [])
        for name in REVIEW_TASK_FLAG_FIELDS
    )


def codex_account(agent: str, explicit: str | None) -> dict:
    """Resolve the label and refuse ambient conflicts before creating any task or worktree."""
    import os

    from lu_machinery.agent_runtime.codex_accounts import (
        CodexAccountRefused,
        codex_account_env,
        resolve_codex_account_label,
    )

    if agent != "codex":
        if explicit is not None:
            raise CodexAccountRefused("codex_account_seat_unsupported")
        return {}
    label = resolve_codex_account_label(explicit)
    codex_account_env(label, os.environ)
    return {"codex_account": label}


def claude_account(agent: str, explicit: str | None) -> dict:
    """The resolved account fields recorded by dispatch v2.

    A claude request resolves its label from ``--claude-account``, the default-account file, then
    the built-in default, and verifies a non-default label's binding and credential sources before
    any worktree exists; a refusal, an unknown label included (``claude_account_unknown``), raises
    ``ValueError`` with the legacy ``ClaudeAccountRefused`` code. Another seat may not name a
    non-default account (``claude_account_seat_unsupported``) and records none. A non-default label
    loads none of its login directory's user settings (:func:`claude_launch`), recorded as
    ``claude_account_user_scope_omitted`` (#10619).
    """
    from lu_machinery.agent_runtime.attempt_boundary import (
        CLAUDE_ACCOUNT_KEY,
        CLAUDE_ACCOUNT_SOURCE_KEY,
        DEFAULT_CLAUDE_ACCOUNT,
        ClaudeAccountRefused,
        resolve_claude_account_label,
        resolve_dispatch_claude_account,
        verify_claude_account,
    )

    try:
        if agent != "claude":
            if resolve_claude_account_label(explicit) != DEFAULT_CLAUDE_ACCOUNT:
                raise ClaudeAccountRefused("claude_account_seat_unsupported")
            # v2 never reselects a claude reviewer for another seat, so no other seat reads the account file.
            return {}
        # ``claude_account_lane.resolve_route_account(explicit, strict=True)`` without a recorded
        # label (v2 has no --force-new re-run): flag, default-account file, built-in default.
        label, source = resolve_dispatch_claude_account(explicit)
        binding = verify_claude_account(label)
    except ClaudeAccountRefused as exc:
        raise ValueError(f"CLAUDE_ACCOUNT_REFUSED: {exc.code}") from None
    fields = {CLAUDE_ACCOUNT_KEY: label, CLAUDE_ACCOUNT_SOURCE_KEY: source}
    if binding is not None:  # v2 launches no sealed review attempt, so every labelled run omits the user scope
        fields["claude_account_user_scope_omitted"] = True
    return fields


def claude_budget(label: str | None, requested: str | None) -> float | None:
    """The spend cap in USD of a dispatch on ``label``, or ``None`` for a subscription label.

    An API-key label always has one (configured amount, or the lower ``--max-budget-usd``); a dispatch
    without a resolvable cap raises ``ValueError`` naming the account, before any worktree exists.
    """
    from lu_machinery.agent_runtime.attempt_boundary import ClaudeAccountRefused, resolve_claude_budget_usd

    try:
        return resolve_claude_budget_usd(label, requested)
    except ClaudeAccountRefused as exc:
        raise ValueError(f"CLAUDE_ACCOUNT_REFUSED: {exc.code} (account {label})") from None


def claude_launch(spec, invocation):
    """Bind a labelled Claude run to its verified login, at the launch boundary (#10619).

    Composes the legacy worker-side checks (``adapters/claude.py`` ``_claude_account_binding`` and
    ``_verified_labelled_launch``) over the v2 environment: the binding is verified again in the
    supervisor and its login directory becomes ``CLAUDE_CONFIG_DIR``; ``claude auth status``, run
    with the launch's own ``--settings``, ``--setting-sources project,local``, working directory and
    environment, must report the bound subscription login and nothing else, so a checkout setting
    such as ``apiKeyHelper`` refuses (``claude_account_auth_method_conflict``); the inputs must not
    change while verified and no machine policy may exist. The returned argv runs the CLI behind
    the legacy launch guard, which recomputes the inputs digest in the spawned process. The default
    label and other seats are returned unchanged. A refusal raises ``ValueError`` with the code.

    An API-key label (C-2643) runs the same way, except that the run's one ``--settings`` (repeats
    are last-wins) becomes the bound directory's own ``settings.json``, whose key helper is then
    the run's only credential; the background denies it replaces are also in ``--disallowedTools``.
    ``auth status`` must report that helper and nothing else, and the file is bound by the digest.
    """
    from lu_machinery.agent_runtime.adapters import claude as adapter
    from lu_machinery.agent_runtime.attempt_boundary import (
        CLAUDE_AUTH_API_KEY_HELPER,
        CLAUDE_AUTH_SUBSCRIPTION_LOGIN,
        DEFAULT_CLAUDE_ACCOUNT,
        ClaudeAccountRefused,
        note_claude_account_verified,
        refuse_claude_machine_policy,
        resolve_claude_account_label,
        verify_claude_account,
    )
    from lu_machinery.agent_runtime.env_sanitize import (
        claude_fingerprints_match,
        claude_labelled_inputs_digest,
        claude_login_credential_fingerprint,
    )
    from lu_machinery.dispatcher.types import Invocation

    if spec.agent != "claude":
        return invocation
    try:
        label = resolve_claude_account_label(spec.claude_account)
        if label == DEFAULT_CLAUDE_ACCOUNT:
            return invocation
        binding = verify_claude_account(label)
        if binding is None:  # only the default label verifies without a binding
            raise ClaudeAccountRefused("claude_account_default_has_no_binding")
        login_dir = str(binding.config_dir)
        env = {**invocation.env, "CLAUDE_CONFIG_DIR": login_dir}
        argv = invocation.argv
        index = argv.index("--settings") + 1
        if binding.api_key:
            argv = [*argv[:index], str(binding.config_dir / "settings.json"), *argv[index + 1:]]
        settings = argv[index]
        sources = argv[argv.index("--setting-sources") + 1]
        if sources != adapter.LABELLED_ACCOUNT_SETTING_SOURCES:
            raise ClaudeAccountRefused("claude_account_settings_changed")

        def digest() -> str:
            return claude_labelled_inputs_digest(
                login_dir=login_dir, cwd=spec.worktree, settings=settings, setting_sources=sources, environ=env
            )

        verified = digest()
        adapter._verify_labelled_auth_method(
            (argv[0],), settings, cwd=spec.worktree, env=env, login_dir=login_dir, api_key=binding.api_key
        )
        # An API key has no login credential; its helper is bound by the digest of its settings file.
        if not claude_fingerprints_match(digest(), verified) or not (
            binding.api_key
            or claude_fingerprints_match(claude_login_credential_fingerprint(login_dir), binding.credential)
        ):
            raise ClaudeAccountRefused("claude_account_settings_changed")
        refuse_claude_machine_policy()
        note_claude_account_verified(
            label, CLAUDE_AUTH_API_KEY_HELPER if binding.api_key else CLAUDE_AUTH_SUBSCRIPTION_LOGIN
        )
    except ClaudeAccountRefused as exc:
        raise ValueError(f"CLAUDE_ACCOUNT_REFUSED: {exc.code}") from None
    guarded = [sys.executable, "-I", "-c", adapter._LAUNCH_GUARD_CODE, verified]
    return Invocation([*guarded, "--", *argv], invocation.stdin, env, invocation.final_file)


class LaunchRefused(ValueError):
    """No supervisor was started, or its start was ambiguous and must not be retried."""

    def __init__(self, message: str, fields: dict):
        super().__init__(message)
        self.fields = fields


def spawn_supervisor(
    cmd: list[str], *, task_id: str, run_nonce: str, cwd: Path, stderr, stderr_log: Path, env: dict[str, str],
    scope_limits: tuple[int, int] | None = None,
):
    """Start the supervisor in ``lu-dispatch.slice`` or, when isolation is unavailable, with plain ``Popen``.

    Uses ``dispatch_isolation.spawn_detached_worker``: the
    returned pid is the supervisor in both cases, and the launch fields (``launch_mode``,
    ``launch_unit``, ``launch_fallback_reason``, ``launch_user_bus``) go into the record. A refused
    fallback (a caller inside a driver scope) or an ambiguous scoped start raises
    :class:`LaunchRefused` with the legacy failure code; it is never relaunched. ``scope_limits``
    (``RuntimeMaxSec``, ``TimeoutStopSec``) bound an orchestrator turn's scope, which never falls back.
    """
    bounded: dict[str, Any] = {} if scope_limits is None else dict(
        allow_fallback=False, runtime_max_s=scope_limits[0], stop_timeout_s=scope_limits[1])
    try:
        process, launch = dispatch_isolation.spawn_detached_worker(
            cmd,
            task_id=task_id,
            run_nonce=run_nonce,
            popen=functools.partial(subprocess.Popen, cwd=cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            stderr_log=stderr_log,
            **bounded,
        )
    except dispatch_isolation.DispatchIsolationError as exc:
        refused = str(exc).startswith("fallback-refused:")
        code = "dispatch_fallback_refused" if refused else "dispatch_isolation_failed"
        message = f"dispatch isolation: {exc}"[:500]
        raise LaunchRefused(message, {"status": "failed", "error": f"{code}: {message}"}) from None
    except (OSError, ValueError) as exc:
        # No supervisor process exists: fail like the legacy spawn instead of reporting a crash.
        message = f"Popen failed: {type(exc).__name__}: {exc}"[:500]
        raise LaunchRefused(message, {"status": "failed", "error": f"worker_spawn_failed: {message}"}) from None
    return process, launch.as_state()
