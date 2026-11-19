"""``ab discuss``: a bounded ACP discussion owned by a Fleet Comms job.

Fleet Comms authority holds the channel, the discussion root, the job and its
terminal receipt; ACP carries the participants' text. The legacy channel,
inbox and broker stores this command once wrote to are removed (#515).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from lu_machinery.common import json_depth

from . import _config
from ._config import DISPATCH_ENTRY

# A channel token as the authoring tree names its directories.
_CHANNEL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def register_discuss_command(subparsers: Any) -> None:
    """Register ``ab discuss`` on the bridge's top-level subparsers."""
    discuss_parser = subparsers.add_parser(
        "discuss",
        help="Multi-agent discussion with round limit (B.4)",
        description=(
            "Multi-agent discussion with round limit (B.4). "
            f"Discussion is analysis-only. Use {DISPATCH_ENTRY} dispatch for execution."
        ),
    )
    discuss_parser.add_argument("channel", help="Channel name")
    discuss_parser.add_argument("body", help="Discussion topic (use '-' for stdin)")
    discuss_parser.add_argument(
        "--with", dest="with_agents", required=True,
        help="Comma-separated list of participating agents",
    )
    discuss_parser.add_argument(
        "--max-rounds", type=int, default=2,
        help="Maximum discussion rounds (default: 2, cap: 3)",
    )
    discuss_parser.add_argument(
        "--review", action="store_true",
        help="Prepend docs/review-protocol.md before channel context",
    )
    discuss_parser.add_argument(
        "--review-profile",
        dest="review_profile",
        choices=("code", "ukrainian"),
        default=None,
        help=(
            "Required when a review discussion includes agy/gemini "
            "(--review, or channel reviews). code is refused "
            "(the bridge refuses code review; native AGY admits low, medium or high risk code reviews). "
            "Ukrainian content review must pass ukrainian."
        ),
    )
    discuss_parser.add_argument(
        "--models",
        default=None,
        help=(
            "Optional per-participant model overrides as agent:model pairs "
            "(comma-separated), e.g. agy:gemini-3.1-pro-high"
        ),
    )
    discuss_parser.add_argument(
        "--efforts",
        default=None,
        help=(
            "Optional per-participant effort overrides as agent:effort pairs "
            "(comma-separated), e.g. codex:high,agy:high"
        ),
    )
    discuss_parser.add_argument(
        "--idempotency-key",
        dest="idempotency_key",
        default=None,
        help=(
            "Explicit idempotency key for the discussion-root message "
            "(default: derived from channel + brief). Pass a new key to "
            "start a fresh root for the same brief without mutating the body"
        ),
    )



def _authoring_context_exists(channel: str) -> bool:
    """Whether the product's authoring tree has a context file for *channel*.

    Such a file must be sealed into Fleet Comms (``<python> -I -m
    lu_machinery.fleet_comms channel context <channel> <body>``) before a discussion uses the
    channel, never silently skipped.
    ``AB_CONTEXT_DIR`` overrides the authoring root. An invalid channel name,
    or a context path that escapes that root, raises ``ValueError``: the
    caller refuses the discussion rather than treating it as "no context".
    """
    if not isinstance(channel, str) or _CHANNEL_NAME.fullmatch(channel) is None:
        raise ValueError(f"channel name must be lowercase-kebab-case; got {channel!r}")
    root = os.path.realpath(os.environ.get("AB_CONTEXT_DIR") or _config.REPO_ROOT / "docs" / "agent-channels")
    candidate = os.path.realpath(os.path.join(root, channel, "context.md"))
    if not candidate.startswith(root + os.sep):
        raise ValueError("channel context path escapes the channel context root")
    return os.path.isfile(candidate)


def _resolve_body(body_arg: str) -> str:
    """Accept '-' to mean 'read from stdin'."""
    if body_arg == "-":
        return sys.stdin.read()
    return body_arg


def _parse_csv(s: str) -> list[str]:
    return [item.strip() for item in s.split(",") if item.strip()]


def _parse_agent_models(s: str) -> dict[str, str]:
    """Parse ``agent:model`` pairs from a comma-separated discuss ``--models`` value."""
    models: dict[str, str] = {}
    for item in _parse_csv(s):
        if ":" not in item:
            raise ValueError(
                f"invalid --models entry {item!r}: expected agent:model (e.g. agy:gemini-3.1-pro-high)"
            )
        agent, model = item.split(":", 1)
        agent = agent.strip()
        model = model.strip()
        if not agent or not model:
            raise ValueError(
                f"invalid --models entry {item!r}: agent and model must both be non-empty"
            )
        models[agent] = model
    return models


def _parse_agent_efforts(s: str) -> dict[str, str]:
    """Parse and validate ``agent:effort`` pairs for ACP discussions."""
    efforts = _parse_agent_models(s)
    allowed = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    invalid = {agent: effort for agent, effort in efforts.items() if effort not in allowed}
    if invalid:
        rendered = ", ".join(f"{agent}:{effort}" for agent, effort in sorted(invalid.items()))
        raise ValueError(f"invalid --efforts value(s): {rendered}")
    return efforts


def _discussion_root_idempotency_key(channel: str, body: str) -> str:
    """Derive the discussion-root key from channel + brief (#7160).

    The authority stores channel and recipient metadata alongside the
    key, so hashing only the brief made a same-brief retry on a fresh
    channel fail with ``idempotency_key_reused_with_different_payload``.
    Scoping the digest to the channel keeps same-channel replays
    deduplicated while fresh-channel retries mint a fresh key.
    """
    digest = hashlib.sha256(f"{channel}\x00{body}".encode()).hexdigest()
    return f"discussion-root:{digest}"

def _gemini_review_request_error(
    *,
    channel: str,
    agents: list[str],
    review: bool,
    profile: str | None,
    force: bool = False,
) -> str | None:
    """Refuse a Gemini code review that arrives without an explicit profile.

    ``post reviews --to agy`` and ``--review`` are review requests. ``discuss
    --with agy`` is always checked: a missing profile names the flag, ``code``
    cites the operator rule, and ``ukrainian`` is allowed.
    """
    from ._gemini_review import GEMINI_REVIEW_AGENTS, gemini_review_profile_error

    if not GEMINI_REVIEW_AGENTS.intersection({agent.strip().lower() for agent in agents}):
        return None
    if not force and not review and channel != "reviews":
        return None
    return gemini_review_profile_error(profile)

def _discussion_context_revisions(authority, channel: str, *, _seen: set[str] | None = None) -> dict[str, str]:
    """Pin Fleet include revisions; unimported authoring files are a visible refusal."""
    from lu_machinery.fleet_comms.authority import AuthorityServiceError

    seen = _seen if _seen is not None else set()
    if channel in seen:
        return {}
    seen.add(channel)
    record = authority.get_channel(channel)
    includes = record.metadata.get("include", [])
    if isinstance(includes, str):
        includes = _parse_csv(includes)
    if not isinstance(includes, list) or any(not isinstance(name, str) for name in includes):
        raise AuthorityServiceError("channel_context_includes_invalid")
    revisions: dict[str, str] = {}
    for name in includes:
        revisions.update(_discussion_context_revisions(authority, name, _seen=seen))
    if record.current_context_revision_id:
        revisions[f"include:{channel}"] = record.current_context_revision_id
    elif _authoring_context_exists(channel):
        raise AuthorityServiceError("channel_context_import_required")
    return revisions


def _read_discussion_context(authority, channel: str, revisions: dict[str, str]) -> str:
    includes = [(name.split(":", 2)[2], revision) for name, revision in sorted(revisions.items())
                if name.startswith("include:")]
    parts = [authority.read_channel_context(name, revision_id=revision) for name, revision in includes]
    if "channel" in revisions and not any(name == channel for name, _ in includes):
        parts.append(authority.read_channel_context(channel, revision_id=revisions["channel"]))
    return "\n".join(part.strip() for part in parts if part.strip())

def handle_discuss(args) -> int:
    """Run bounded ACP discussion with a Fleet root and its pinned context.

    Fleet owns the discussion job and terminal receipt. ACP transports the
    participants' text; it never falls back to legacy channel execution.
    """
    # Lazy import: agent_runtime pulls in the adapter stack and we
    # don't want that on every ``ab --help``.
    try:
        from lu_machinery.agent_runtime.attribution import resolve_invocation_attribution
    except ImportError as e:
        print(f"❌ agent_runtime unavailable: {e}", file=sys.stderr)
        print(
            "   ab discuss needs the runtime adapters to be importable.",
            file=sys.stderr,
        )
        return 1

    # ── the rules core first ──────────────────────────────────────
    # Every discussion leg starts with it: refuse before the channel is
    # created, the root is published or the discussion is enqueued (the
    # discussion controller re-checks).
    from lu_machinery.lib import rules_core

    try:
        rules_core.require_core()
    except rules_core.RulesCoreMissing as exc:
        print(f"❌ discussion refused: {exc}; the rules core is required", file=sys.stderr)
        return 1

    # ── validate inputs ────────────────────────────────────────────
    requested_transport = os.environ.get("LU_AGENT_COMM_TRANSPORT", "acp").strip().lower()
    if requested_transport != "acp":
        print(
            "❌ normal inter-agent communication requires LU_AGENT_COMM_TRANSPORT=acp; "
            "the bridge provider transport is retired",
            file=sys.stderr,
        )
        return 1
    from lu_machinery.agent_runtime.acpx_discuss import MAX_PARTICIPANTS, MIN_PARTICIPANTS
    from lu_machinery.agent_runtime.adapters.acpx import ACPX_SUPPORTED_PARTICIPANTS

    acp_participant_routes = ACPX_SUPPORTED_PARTICIPANTS
    with_agents = _parse_csv(args.with_agents)
    agent_models: dict[str, str] = {}
    if getattr(args, "models", None):
        try:
            agent_models = _parse_agent_models(args.models)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
    # Kimi seats never join discussions: one gate on the effective seats and
    # models (--models overrides included) before any channel write.
    from lu_machinery.agent_runtime.acpx_discuss import AcpxDiscussionError, refuse_kimi_discussion

    try:
        refuse_kimi_discussion(with_agents, agent_models)
    except AcpxDiscussionError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    review_error = _gemini_review_request_error(
        channel=args.channel,
        agents=with_agents,
        review=bool(getattr(args, "review", False)),
        profile=getattr(args, "review_profile", None),
        force=True,
    )
    if review_error is not None:
        print(f"❌ {review_error}", file=sys.stderr)
        return 2
    if not with_agents:
        print(
            f"❌ --with requires {MIN_PARTICIPANTS} to {MAX_PARTICIPANTS} "
            "distinct enabled ACP seats",
            file=sys.stderr,
        )
        return 1
    allowed_acp_seats = tuple(sorted(acp_participant_routes))
    if (
        not MIN_PARTICIPANTS <= len(with_agents) <= MAX_PARTICIPANTS
        or len(set(with_agents)) != len(with_agents)
        or any(agent not in acp_participant_routes for agent in with_agents)
    ):
        print(
            "❌ unsupported ACP discussion seat list: "
            f"{', '.join(with_agents)}; requested {MIN_PARTICIPANTS} to "
            f"{MAX_PARTICIPANTS} distinct enabled ACP seats "
            f"(allowed: {', '.join(allowed_acp_seats)})",
            file=sys.stderr,
        )
        return 1
    if agent_models:
        unknown_models = [a for a in agent_models if a not in with_agents]
        if unknown_models:
            print(
                "❌ --models names agent(s) not in --with: "
                f"{', '.join(unknown_models)}",
                file=sys.stderr,
            )
            return 1

    agent_efforts: dict[str, str] = {}
    if getattr(args, "efforts", None):
        try:
            agent_efforts = _parse_agent_efforts(args.efforts)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        unknown_efforts = [a for a in agent_efforts if a not in with_agents]
        if unknown_efforts:
            print(
                "❌ --efforts names agent(s) not in --with: "
                f"{', '.join(unknown_efforts)}",
                file=sys.stderr,
            )
            return 1

    MAX_ROUNDS_CAP = 3
    max_rounds = min(max(1, args.max_rounds), MAX_ROUNDS_CAP)
    if args.max_rounds > MAX_ROUNDS_CAP:
        print(
            f"ℹ️  clamping --max-rounds {args.max_rounds} to {MAX_ROUNDS_CAP} "
            f"(hard cap — escalate to a human if 3 rounds don't converge)"
        )

    body = _resolve_body(args.body)
    if not body.strip():
        print("❌ empty discussion topic", file=sys.stderr)
        return 1
    explicit_idempotency_key = (getattr(args, "idempotency_key", None) or "").strip()
    if getattr(args, "idempotency_key", None) is not None and not explicit_idempotency_key:
        print("❌ --idempotency-key must not be empty", file=sys.stderr)
        return 1
    root_idempotency_key = explicit_idempotency_key or _discussion_root_idempotency_key(
        args.channel, body
    )
    caller_attribution = resolve_invocation_attribution()
    runtime_initiator = (
        caller_attribution.initiator
        if caller_attribution.initiator != "unknown"
        else None
    )
    discussion_source = runtime_initiator or "operator"

    # The job owns participant execution, so the root creates no deliveries.
    from lu_machinery.fleet_comms.authority import AuthorityService

    authority = AuthorityService()
    try:
        authority.create_channel(args.channel)
        snapshot = _discussion_context_revisions(authority, args.channel)
        context_revisions = {
            f"include:{index:06d}:{name.removeprefix('include:')}": revision
            for index, (name, revision) in enumerate(snapshot.items())
        }
        if f"include:{args.channel}" in snapshot:
            context_revisions["channel"] = snapshot[f"include:{args.channel}"]
        root = authority.publish_message(
            sender=discussion_source,
            body=body,
            channel=args.channel,
            recipients=(),
            kind="discussion-root",
            context_revisions=context_revisions,
            provenance={
                "Source": discussion_source,
                "Agent": "ab-discuss",
                "Via": "acp",
            },
            idempotency_key=root_idempotency_key,
        )
        root_id = root.message_id
        correlation_id = root.thread_id
        channel_context = _read_discussion_context(authority, args.channel, root.context_revisions)
        deadline_at = (datetime.now(UTC) + timedelta(seconds=1800)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task_channel = re.sub(r"[^A-Za-z0-9._:-]+", "-", args.channel).strip("-._:")
        task_digest = hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()[:10]
        acp_task_id = f"discuss-{task_channel[:48] or 'channel'}-{task_digest}"
        from lu_machinery.agent_runtime.acpx_discuss import CONTENT_BUDGET_BYTES, TOKEN_BUDGET

        discussion_budget = {
            "token_budget": TOKEN_BUDGET,
            "content_budget_bytes": CONTENT_BUDGET_BYTES,
        }
        authority_job = authority.enqueue_discussion(
            channel=args.channel,
            prompt=body,
            participants=with_agents,
            rounds=max_rounds,
            task_digest=hashlib.sha256(acp_task_id.encode("utf-8")).hexdigest(),
            correlation_id=correlation_id,
            deadline_at=deadline_at,
            idempotency_key=f"ab-discuss:{correlation_id}",
            source=discussion_source,
            task_id=acp_task_id,
            **discussion_budget,
        )
        if authority_job.state in {"complete", "failed", "expired", "dead_lettered"}:
            replay = authority.read_job_result(authority_job.job_id)
            if authority_job.state != "complete" or replay is None:
                print("❌ prior ACP discussion is terminal and failed", file=sys.stderr)
                return 1
            payload = json_depth.loads(replay, max_depth=512)
            lease = None
        else:
            lease = authority.claim_job(
                authority_job.job_id,
                f"ab-discuss:{os.getpid()}",
                lease_seconds=1830,
            )
    except (ValueError, RuntimeError) as e:
        print(f"❌ post failed: {e}", file=sys.stderr)
        return 1
    finally:
        authority.close()
    print(
        f"📢 discuss #{args.channel} (max {max_rounds} round{'s' if max_rounds > 1 else ''}, "
        f"{len(with_agents)} participants)"
    )
    print(f"   root message: {root_id[:12]} / thread {correlation_id[:12]}")
    print()

    # ACP is the only provider transport for normal inter-agent discussion.
    # Participant admission above runs before authority writes, so a malformed
    # seat list cannot create a conversation that the read-only status surface
    # would later need to normalize. A refusal, partial result, timeout, or
    # cancellation never replays the provider call over the retired bridge path.
    from lu_machinery.agent_runtime.acpx_discuss import run_discussion

    from ._acp_compat import _discussion_failure_metadata, _failure_metadata
    from ._acp_execution import acp_execution_cwd

    acp_rounds = max_rounds
    # A replayed terminal discussion (no lease) reuses its stored payload.
    if lease is not None:
        try:
            # ACP already persists and exchanges the current conversation.
            # Keep a bounded slice of stable channel context plus the exact root.
            if len(channel_context) > 24_000:
                channel_context = channel_context[:24_000] + "\n[context truncated]"
            acp_prompt = (
                (f"--- pinned channel context ---\n{channel_context}\n\n" if channel_context else "")
                + f"--- discussion task ---\n{body}\n\n"
                + "Produce an independent first take from the supplied text only. "
                + "No tools, files, terminal, or external lookup are available."
            )
            previous_transport = os.environ.get("LU_ACPX_TRANSPORT")
            os.environ["LU_ACPX_TRANSPORT"] = "active"
            try:
                with acp_execution_cwd(_config.REPO_ROOT, task_id=acp_task_id) as execution_cwd:
                    payload = run_discussion(
                        prompt=acp_prompt,
                        cwd=execution_cwd,
                        task_id=acp_task_id,
                        correlation_id=correlation_id,
                        idempotency_key=f"ab-discuss:{correlation_id}",
                        rounds=acp_rounds,
                        participants=tuple(with_agents),
                        models=agent_models or None,
                        efforts=agent_efforts or None,
                        source=discussion_source,
                        initiator=discussion_source,
                        reserved_conversation_id=authority_job.subject_id,
                    )
            finally:
                if previous_transport is None:
                    os.environ.pop("LU_ACPX_TRANSPORT", None)
                else:
                    os.environ["LU_ACPX_TRANSPORT"] = previous_transport
        except Exception as exc:
            with AuthorityService() as failed_authority:
                failed_authority.finish_job(
                    authority_job.job_id,
                    worker_id=f"ab-discuss:{os.getpid()}",
                    fence_token=lease.fence_token,
                    state="failed",
                    result=json.dumps(
                        {"error": type(exc).__name__, "transport": "acp"},
                        sort_keys=True,
                    ).encode("utf-8"),
                    failure=_failure_metadata(error=exc),
                )
            print(f"❌ ACP discussion failed without bridge retry: {exc}", file=sys.stderr)
            return 1

    conversation_id = str(payload["conversation_id"])
    state = str(payload["state"])
    synthesis = str(payload.get("synthesis") or "[no synthesis available]")
    if lease is not None:
        with AuthorityService() as completed_authority:
            completed_authority.finish_job(
                authority_job.job_id,
                worker_id=f"ab-discuss:{os.getpid()}",
                fence_token=lease.fence_token,
                state="complete" if state == "COMPLETE" else "failed",
                result=json.dumps(payload, sort_keys=True).encode("utf-8"),
                failure=(
                    None
                    if state == "COMPLETE"
                    else _discussion_failure_metadata(payload)
                ),
            )
            completed_authority.publish_message(
                sender="acp-controller",
                body=f"[ACP {state}] conversation={conversation_id}\n\n{synthesis}",
                channel=args.channel,
                kind="discussion-result",
                conversation_id=root.conversation_id,
                in_reply_to=root_id,
                correlation_id=correlation_id,
                provenance={
                    "Source": "acp",
                    "Agent": "controller",
                    "Via": "fleet-comms",
                },
                idempotency_key=f"discussion-result:{authority_job.job_id}",
            )
    print(f"   transport: ACP ({', '.join(with_agents)})")
    print(f"   conversation: {conversation_id}")
    print(f"   state: {state}; rounds completed: {payload.get('rounds_completed', 0)}")
    return 0 if state == "COMPLETE" else 1
