"""
FastAPI server for playground dashboards.

Architecture:
  - main.py: shared endpoints (config, static files, health, orient)
  - batch_router.py: batch state, dispatcher, and websocket
"""

import asyncio
import functools
import html
import importlib.metadata
import logging
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import Metric
from prometheus_client.exposition import CONTENT_TYPE_PLAIN_0_0_4
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import lu_machinery.github_rest_cache as github_rest
from lu_machinery.agent_runtime.usage import has_headroom
from lu_machinery.common import json_depth
from lu_machinery.common.jsonl import physical_lines
from lu_machinery.common.release_layout import is_release_root
from lu_machinery.common.repo_root import ProductRootError, machinery_path
from lu_machinery.control_plane.storage import StoreId, assert_component_supported
from lu_machinery.github_check_rollup import group_collapsed_by_name
from lu_machinery.guardrails import worktree_containment
from lu_machinery.orientation_packet import PACKET_MAX_BYTES, cap_response, json_bytes
from lu_machinery.research import registry as reg

try:
    from lu_machinery.path_safety import safe_join  # scripts/ on sys.path (test sys.path-hack)
except ImportError:
    from ..path_safety import safe_join  # scripts.api package import (production)

from lu_machinery.orchestration import issue_stream_audit as isa
from lu_machinery.orchestration.fleet_repos import FleetRepoError, load_fleet_repos

from . import config, subscriptions
from . import delegate_router as delegate_api
from . import runtime_router as runtime_api
from . import state_router as state_api
from ._signal_log import install_signal_logging
from .cluster_router import router as cluster_router
from .codexbar_usage import scheduler_status, start_periodic_refresh, stop_periodic_refresh
from .comms_prometheus import CommsCollector
from .comms_router import refresh_panel_metrics_periodically, warm_panel_metrics
from .comms_router import router as comms_router
from .coordination_router import router as coordination_router
from .delegate_router import router as delegate_router
from .docs_router import router as docs_router
from .epics_router import router as epics_router
from .epics_router import seed_manifest_inventory
from .fleet_router import router as fleet_router
from .fleet_workers_router import router as fleet_workers_router
from .git_hygiene_router import router as git_hygiene_router
from .git_hygiene_router import warm_pull_requests
from .issues_router import router as issues_router
from .knowledge_router import router as knowledge_router
from .milestones_router import router as milestones_router
from .model_stats_router import ModelStatsCollector
from .model_stats_router import router as model_stats_router
from .monitor_context import MonitorContext, get_ctx, production_context, resolve_context
from .observer_presence import router as observer_presence_router
from .occupancy import router as occupancy_router
from .occupancy_local import resolve_launcher_host_id
from .ops_router import router as ops_router
from .opsec_sanitize import opsec_path_sanitizer_middleware
from .preload import preload_all
from .product_contribution import (
    PRODUCT_DATA,
    OrientSection,
    ProductContribution,
    install_providers,
    load_product,
    product_file,
)
from .project_state_router import router as project_state_router
from .repository_authority import build_repository_authority, cwd_role
from .resilience import get_resilience_snapshot, resilience_middleware
from .rollover_router import collect_rollover_orient_data
from .route_contracts import PAGE_CONTRACTS, PageContract
from .rules_router import router as rules_router
from .runtime_router import router as runtime_router
from .session_router import router as session_router
from .session_streams_router import router as session_streams_router
from .state_helpers import Demand, PeriodicSnapshot, cache_get, cache_invalidate, cache_set, ctx_cache_scope
from .state_router import router as state_router
from .state_router import schedule_state_scan_warmup
from .telemetry.response import add_json_telemetry, session_id_from_request
from .telemetry_router import router as telemetry_router
from .work_router import drain_context_background_work, refresh_projection_cache_periodically, warm_projection_cache
from .work_router import router as work_router
from .worktrees_router import router as worktrees_router

# Read-only Git children (status refreshing the index) must never take
# index.lock: a request killed mid-call would leave it behind (#8874). Every
# child inherits this; an explicit operator value wins.
os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")

core_router = APIRouter()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Lifespan hook — wrap uvicorn signal handlers so we record WHO killed us.

    Without this, "Shutting down" lines in logs/api.log have no provenance.
    See scripts/api/_signal_log.py for the wrapper rationale.

    Startup uses the authority-service interlock. An authority that component
    cannot write refuses startup, so plane-status cannot report a store the
    seats do not write. Sqlite and shadow stay allowed.
    """
    ctx = _app.state.ctx
    product: ProductContribution = _app.state.product
    projection_refresh_task = None
    panel_refresh_task = None
    orient_refresh_task = None
    try:
        # Reuse assert_component_supported; do not add a second allow-list.
        # ControlPlaneUnsupportedComponentError names the store and authority
        # and carries no connection string.
        assert_component_supported(StoreId.FLEET_COMMS, "authority_service")
        preload_all(product.preload, product.preload_optional)
        install_signal_logging()
        warm_panel_metrics(ctx)
        warm_pull_requests(ctx)
        seed_manifest_inventory(
            ctx.roots.project_root,
            store=ctx.stores.epics_store,
            handoff_root=ctx.roots.live_repo_root,
            ctx=ctx,
        )
        try:
            isa.schedule_refresh(force=False)
        except Exception as exc:
            logger.warning("Issue stream audit refresh schedule on startup failed: %s", exc)
        try:
            warm_projection_cache(ctx=ctx)
        except Exception as exc:
            logger.warning("Work projection warmup schedule on startup failed: %s", exc)
        try:
            schedule_state_scan_warmup(ctx, product.warmup)
        except Exception as exc:
            logger.warning("State scan warmup schedule on startup failed: %s", exc)
        projection_refresh_task = asyncio.create_task(
            refresh_projection_cache_periodically(ctx), name="work-projection-refresh"
        )
        panel_refresh_task = asyncio.create_task(refresh_panel_metrics_periodically(ctx), name="comms-metrics-refresh")
        orient_refresh_task = asyncio.create_task(refresh_orient_periodically(ctx, product), name="orient-refresh")
        start_periodic_refresh()
        yield
    finally:
        if orient_refresh_task is not None:
            orient_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await orient_refresh_task
        if panel_refresh_task is not None:
            panel_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await panel_refresh_task
        if projection_refresh_task is not None:
            projection_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await projection_refresh_task
        try:
            stop_periodic_refresh()
        finally:
            try:
                await drain_context_background_work(ctx)
            finally:
                await ctx.runtime.close_resources()


def _error_envelope(
    error: str, detail: Any, *, status_code: int, headers: dict[str, str] | None = None, bounded: bool = False
) -> JSONResponse:
    """Build the public error shape without serializing exception text.

    Route-specific ``HTTPException`` details remain available when they are
    already structured or short, stable API messages. Unhandled exceptions
    never pass their ``str(exc)`` representation to a client; the correlation
    id is the only diagnostic handle exposed on the wire.
    """
    content = {"error": error, "error_id": uuid.uuid4().hex, "detail": detail}
    if bounded:
        content = cap_response(content, locator="/api/work/v1/projection")
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content=content,
    )


def _sanitize_public_detail(detail: Any) -> Any:
    """Retain stable API details while dropping raw exception-shaped values."""
    if isinstance(detail, dict):
        return {str(key): _sanitize_public_detail(value) for key, value in detail.items() if key != "input"}
    if isinstance(detail, list):
        return [_sanitize_public_detail(value) for value in detail]
    if not isinstance(detail, str):
        return detail

    suspicious = (
        "Traceback (most recent call last)",
        "No such file or directory",
        "OperationalError:",
        "FileNotFoundError:",
        "PermissionError:",
        "ValueError:",
        "RuntimeError:",
        "status_failed:",
        "digest_failed:",
        "drift_failed:",
    )
    if any(marker in detail for marker in suspicious):
        return "request rejected"
    if re.search(
        r"(?<![A-Za-z0-9/])/(?:home|Users|Volumes|private|opt|srv|tmp|var)(?:/[^\s\"'<>]*)?",
        detail,
    ):
        return "request rejected"
    return detail


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Normalize handled HTTP errors without changing their status codes."""
    return _error_envelope(
        "http_error",
        _sanitize_public_detail(exc.detail),
        status_code=exc.status_code,
        headers=exc.headers,
        bounded=request.url.path == "/api/work/v1/projection",
    )


async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return validation shape without echoing submitted values."""
    sanitized_errors = []
    for error in exc.errors():
        sanitized = {key: _sanitize_public_detail(value) for key, value in error.items() if key not in {"ctx", "input"}}
        sanitized_errors.append(sanitized)
    return _error_envelope(
        "validation_error", sanitized_errors, status_code=422, bounded=request.url.path == "/api/work/v1/projection"
    )


async def global_exception_handler(request: Request, exc: Exception):
    """Consistent JSON error format for unhandled exceptions."""
    error_id = uuid.uuid4().hex
    logger.error("Unhandled Monitor API exception [%s]", error_id, exc_info=exc)
    del request
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "error_id": error_id,
            "detail": "internal server error",
        },
    )


# Server start time for uptime calculation
_SERVER_START = datetime.now(UTC)


@functools.cache
def _machinery_version() -> str | None:
    """The installed lu-machinery release this server runs, or None when no distribution is installed."""
    try:
        return importlib.metadata.version("lu-machinery")
    except importlib.metadata.PackageNotFoundError:
        return None


# --- /api/orient caching + failure isolation (GH #1309) ----------------
#
# Per-section TTLs (seconds). Tuned for each collector's cost + change
# frequency. A section's stored value is ``hit`` (current) while younger
# than its TTL; an older one is served labelled ``stale`` with its own
# ``generated_at`` while it refreshes (``_cached_detached_orient_section``).
#
# A TTL of ``0`` means "no freshness window at the orient layer": a stored
# value is never labelled current, so every read refreshes it. Use it for
# sections that already carry their own downstream cache (e.g. ``pipeline``
# wraps ``/api/state/summary`` which has its own 60 s TTL — an orient-layer
# window on top would stack the two and label up-to-119 s old data as
# fresh, reviewer BLOCKER #1309).
#
# The section deadlines bound what the request waits for. A collector runs
# on its own thread, including async collectors with their own event loops.
# If it outlives a request, it keeps its context's permit until it finishes.
ORIENT_SECTION_TTLS: dict[str, float] = {
    "git": 30.0,
    "issues": 120.0,
    "idle_prs": 60.0,
    "runtime": 60.0,
    "delegate": 30.0,
    "capacity": 15.0,
    # One rollover audit costs about 5 s of CPU, and rollovers change rarely.
    "rollovers": 120.0,
    "health": 15.0,
    "session_hints": 60.0,
}

ORIENT_SECTION_SOURCES: dict[str, str] = {
    "git": "git",
    "issues": "gh",
    "idle_prs": "gh",
    "runtime": "fs",
    "delegate": "fs",
    "capacity": "fs",
    "rollovers": "fs",
    "health": "probe",
    "session_hints": "fs",
}

ORIENT_SECTION_HARD_TIMEOUT_S = 5.0

IDLE_PR_THRESHOLD_S = 60 * 60
# Only the detached worker waits on this read, never a request. One conditional
# read per open PR takes several seconds (7-8 s for 11 PRs), so a 4 s bound
# failed every attempt and retried the whole fan-out every 30 s.
IDLE_PR_FETCH_TIMEOUT_S = 60.0
IDLE_PR_REPOSITORY = "learn-ukrainian/learn-ukrainian.github.io"
IDLE_PR_CACHE_KEY = "orient_idle_prs"
IDLE_PR_SUCCESSFUL_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
IDLE_PR_ADVISORY_MARKER = "advisory"
_CROSS_FAMILY_REVIEW_RE = re.compile(r"\bcross[- ]family\b", re.IGNORECASE)
_REVIEW_PASS_RE = re.compile(r"\b(?:approve(?:d)?|pass(?:ed)?)\b", re.IGNORECASE)
_REVIEW_BLOCK_RE = re.compile(
    r"\b(?:changes?[- ]requested|needs?[- ]work|blocked|fail(?:ed|ure)?)\b",
    re.IGNORECASE,
)
_REVIEW_HEAD_RE = re.compile(
    r"\b(?:at\s+)?head\b\s*(?:[:=]\s*)?`?([0-9a-f]{40})`?",
    re.IGNORECASE,
)

# Machinery sections only. The product contribution adds pipeline, wiki and
# governance (``ProductContribution.orient_sections``); ``_orient_layout``
# composes both in the response order the endpoint always had. Pipeline has
# TTL 0 on purpose: its collector wraps ``/api/state/summary``, which has its
# own 60 s cache, and stacking both labelled up-to-119 s old data as fresh
# (reviewer BLOCKER #1309 / B2).
ORIENT_SECTION_KEYS: tuple[str, ...] = tuple(ORIENT_SECTION_TTLS.keys())

# Lean cold-start preset (``?lean=true``): the small, lane-agnostic sections an agent needs to
# orient BEFORE it has selected work — tree state (git), active dispatches (delegate),
# blocking decisions (governance), health, and the handoff pointers / current goal
# (session_hints). Excludes the three heavy sections — ``pipeline`` (~2k module stats),
# ``issues`` (full gh list), ``wiki`` (per-track coverage) — which are fetched on demand via
# ``?sections=...``. ``capacity``, ``health``, ``delegate``, and ``rollovers`` stay in the
# preset but are cache-first / detached so a hung collector cannot pin the gather at 5 s
# (#6983, #10866).
LEAN_ORIENT_SECTIONS: tuple[str, ...] = (
    "git",
    "runtime",
    "delegate",
    "capacity",
    "rollovers",
    "health",
    "session_hints",
)

# Lean ``capacity``, ``health``, ``delegate``, ``rollovers`` and the full
# orient's ``issues`` are cache-first and detached: CodexBar, integrity
# canaries, GitHub, and hung delegate/rollover collectors must not pin
# ``asyncio.gather`` at the 5 s section wall (#6983, #10866). A short join lets
# cheap collectors populate the same request. A collector still running after
# the wait can only contribute a previously measured value.
DETACHED_ORIENT_SECTION_KEYS: frozenset[str] = frozenset({"capacity", "health", "delegate", "rollovers", "issues"})
DETACHED_ORIENT_INLINE_WAIT_S = 0.4
# Each context limits concurrent slow scans to protect request threads from
# GIL contention. Collectors can outlive a request or app shutdown, so the
# permits belong to the context that owns the work.
DETACHED_ORIENT_COLLECT_LIMIT = 2
ORIENT_REFRESH_INTERVAL_S = 10.0
_orient_clock: Callable[[], float] = time.monotonic


@dataclass
class _OrientState:
    """Collection and snapshot state for one Monitor context."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    slots: threading.BoundedSemaphore = field(
        default_factory=functools.partial(threading.BoundedSemaphore, DETACHED_ORIENT_COLLECT_LIMIT),
    )
    refresh_threads: dict[str, threading.Thread] = field(default_factory=dict)
    fresh_threads: dict[str, threading.Thread] = field(default_factory=dict)
    # Collection start time orders concurrent publishes.
    values: dict[str, tuple[Any, str, float]] = field(default_factory=dict)
    errors: dict[str, tuple[str, float]] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)
    demand: dict[str, Demand] = field(default_factory=dict)


def _orient_state(ctx: MonitorContext) -> _OrientState:
    state: _OrientState = ctx.runtime.get_or_create_derived("orient-state", _OrientState)
    return state

# The idle-PR feed is deliberately cache-first. A live GitHub request is run by
# one detached daemon worker, never by the ASGI request path. The last successful
# result remains available as a stale fallback when GitHub is slow or unavailable.
_idle_pr_refresh_lock = threading.Lock()
_idle_pr_refresh_threads: dict[str, threading.Thread] = {}
_idle_pr_last_good: dict[str, tuple[dict[str, Any], str]] = {}
_idle_pr_last_error: dict[str, str] = {}
_idle_pr_next_retry_at: dict[str, float] = {}


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _run_command(
    args: list[str], *, timeout: float = 2.0, ctx: MonitorContext | None = None
) -> subprocess.CompletedProcess[str]:
    resolved_ctx = resolve_context(ctx)
    cwd = resolved_ctx.roots.live_repo_root if args and args[0] == "git" else resolved_ctx.roots.project_root
    if not Path(cwd).exists():
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr="cwd does not exist")
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, AssertionError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr=str(exc))


# The git section's five cheap reads are independent subprocesses that spend
# their time waiting, not computing. Run together they cost the slowest one
# instead of the sum, which matters most while detached collectors hold the GIL.
_GIT_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=5, thread_name_prefix="orient-git")


def _collect_git_orient_data(ctx: MonitorContext | None = None) -> dict:
    resolved_ctx = resolve_context(ctx)
    git = ["git", "--no-optional-locks"]
    probes = [
        _GIT_PROBE_EXECUTOR.submit(_run_command, [*git, *args], ctx=ctx)
        for args in (
            ("branch", "--show-current"),
            ("rev-parse", "--short=9", "HEAD"),
            ("rev-parse", "HEAD"),
            ("rev-list", "--count", "origin/main..HEAD"),
            ("log", "--oneline", "-5"),
        )
    ]
    branch_proc, head_proc, full_head_proc, ahead_proc, log_proc = (probe.result() for probe in probes)

    if branch_proc.returncode != 0:
        raise RuntimeError(branch_proc.stderr.strip() or "git branch failed")
    if head_proc.returncode != 0:
        raise RuntimeError(head_proc.stderr.strip() or "git rev-parse failed")
    if log_proc.returncode != 0:
        raise RuntimeError(log_proc.stderr.strip() or "git log failed")

    recent_commits = []
    # One ``--oneline`` record per physical line; a subject may contain NEL/LS/PS.
    for line in physical_lines(log_proc.stdout):
        line = line.strip()
        if not line:
            continue
        sha, _, subject = line.partition(" ")
        recent_commits.append({"sha": sha, "subject": subject})

    ahead_value = 0
    if ahead_proc.returncode == 0:
        try:
            ahead_value = int(ahead_proc.stdout.strip() or "0")
        except ValueError:
            ahead_value = 0
    # The two slow reads stay serial, status first. The status read heals a
    # bare-drifted primary (a Git configuration write, which can also switch on
    # a linked worktree's own config), and the authority read must see the
    # checkout as that heal leaves it.
    try:
        primary_status = worktree_containment.primary_checkout_dirty_status(resolved_ctx.roots.live_repo_root)
    except Exception:
        branch = branch_proc.stdout.strip()
        primary_status = {
            "role": "primary",
            "head_sha": (full_head_proc.stdout.strip() if full_head_proc.returncode == 0 else None),
            "branch": branch,
            "protected_branch": branch in worktree_containment.PROTECTED_BRANCHES,
            "dirty": False,
            "dirty_count": 0,
        }

    branch = branch_proc.stdout.strip()
    authority = build_repository_authority(
        project_root=resolved_ctx.roots.project_root,
        live_repo_root=resolved_ctx.roots.live_repo_root,
    )
    return {
        "branch": branch,
        "head": head_proc.stdout.strip(),
        "ahead_of_origin": ahead_value,
        "recent_commits": recent_commits,
        "primary_checkout_dirty": primary_status["dirty"],
        "primary_checkout": {
            "role": primary_status.get("role", "primary"),
            "head_sha": primary_status.get("head_sha")
            or (full_head_proc.stdout.strip() if full_head_proc.returncode == 0 else None),
            "dirty_count": int(primary_status.get("dirty_count", 0)),
        },
        "cwd_role": cwd_role(Path(resolved_ctx.roots.live_repo_root)),
        "authority": authority,
    }


def _collect_issues_orient_data(ctx: MonitorContext | None = None) -> dict:
    """Fetch open GitHub issues via conditional REST.

    Raises ``RuntimeError`` on any failure. Raising is important —
    ``_cached_orient_section`` only caches successful returns, so a
    transient GitHub blip must not poison the issues cache for the full
    TTL window (reviewer BLOCKER, GH #1309). Timeouts surface as
    ``RuntimeError`` the same way a failed ``gh`` subprocess did.
    """
    issue_limit = 10
    try:
        payload = github_rest.list_open_issues(IDLE_PR_REPOSITORY, limit=issue_limit, timeout=5.0)
    except github_rest.GitHubRestTimeout as exc:
        raise RuntimeError(str(exc)) from exc
    except github_rest.GitHubRestError as exc:
        raise RuntimeError(str(exc)) from exc
    if getattr(payload, "truncated", False) and len(payload) < issue_limit:
        raise RuntimeError("GitHub issue list truncated")

    now = datetime.now(UTC)
    issues = []
    for item in payload:
        created = _parse_iso_datetime(item.get("createdAt"))
        labels = item.get("labels") or []
        issues.append(
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "labels": [label.get("name") for label in labels if isinstance(label, dict) and label.get("name")],
                "age_days": max(0, (now.date() - created.date()).days) if created else None,
            }
        )
    return {"issues": issues}


def _idle_check_row_green(check: dict[str, Any]) -> bool:
    status = str(check.get("status") or "").upper()
    if status and status not in {"COMPLETED", "SUCCESS"}:
        return False
    outcome = str(check.get("conclusion") or check.get("state") or "").upper()
    return outcome in IDLE_PR_SUCCESSFUL_CONCLUSIONS


def _idle_pr_checks_green(pr: dict[str, Any]) -> bool:
    """Return whether every surviving non-advisory check for a PR is green.

    Historical runs collapse through :func:`group_collapsed_by_name`. A name
    stays red or pending when any surviving row is: a later success in another
    workflow does not hide an earlier failure, and a timestamp tie keeps both.
    Missing or malformed status data fails closed. Explicitly advisory checks
    follow the repository merge-hook convention and do not block eligibility.
    """
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return False

    named, other = group_collapsed_by_name(rollup)
    if other or not named:
        return False

    blocking_count = 0
    for name, rows in named.items():
        if IDLE_PR_ADVISORY_MARKER in name.casefold():
            continue
        blocking_count += 1
        if any(not _idle_check_row_green(row) for row in rows):
            return False

    return blocking_count > 0


def _idle_pr_has_review_gate(pr: dict[str, Any]) -> bool:
    """Return whether a proven review decision or a review comment clears the gate.

    One GitHub ``APPROVED`` review is not enough. REST cannot see how many
    approvals branch protection requires, so eligibility needs ``reviewDecision``
    ``APPROVED`` (only when that state is known) or an explicit cross-family
    comment. A missing decision fails closed.
    """
    if str(pr.get("reviewDecision") or "").upper() == "APPROVED":
        return True

    head_sha = pr.get("headRefOid")
    comments = pr.get("comments")
    if not isinstance(comments, list):
        return False
    ordered_comments = sorted(
        (comment for comment in comments if isinstance(comment, dict)),
        key=lambda comment: _parse_iso_datetime(comment.get("createdAt")) or datetime.min.replace(tzinfo=UTC),
    )
    for comment in reversed(ordered_comments):
        body = comment.get("body")
        if not isinstance(body, str) or not _CROSS_FAMILY_REVIEW_RE.search(body):
            continue
        head_match = _REVIEW_HEAD_RE.search(body)
        if (
            head_match is not None
            and isinstance(head_sha, str)
            and head_match.group(1).casefold() != head_sha.casefold()
        ):
            continue
        if _REVIEW_BLOCK_RE.search(body):
            return False
        return _REVIEW_PASS_RE.search(body) is not None
    return False


def _eligible_idle_pr(pr: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    if pr.get("state") is not None and str(pr.get("state")).upper() != "OPEN":
        return None
    merge_state = pr.get("mergeStateStatus")
    if merge_state is not None and str(merge_state).upper() == "DIRTY":
        return None
    if pr.get("isDraft") is True or not _idle_pr_checks_green(pr):
        return None
    if not _idle_pr_has_review_gate(pr):
        return None

    number = pr.get("number")
    branch = pr.get("headRefName")
    updated_at = _parse_iso_datetime(pr.get("updatedAt"))
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number <= 0
        or not isinstance(branch, str)
        or not branch
        or updated_at is None
    ):
        return None

    idle_seconds = (now - updated_at).total_seconds()
    if idle_seconds <= IDLE_PR_THRESHOLD_S:
        return None
    return {
        "number": number,
        "branch": branch,
        "minutes_idle": max(0, int(idle_seconds // 60)),
    }


def _collect_idle_prs_orient_data(ctx: MonitorContext | None = None) -> dict[str, Any]:
    """Fetch and filter the compact green+reviewed+idle PR projection.

    This function is only called by the detached refresh worker. The endpoint
    itself uses ``_cached_idle_pr_section`` and never waits for GitHub.
    Check runs, reviews, and issue comments are conditional REST reads bounded
    by the open PR count. A timeout raises ``subprocess.TimeoutExpired`` so the
    worker reports ``gh_timeout``. Other failures raise ``RuntimeError`` and
    are not cached.
    """
    try:
        payload = github_rest.list_open_prs(
            IDLE_PR_REPOSITORY,
            limit=1000,
            timeout=IDLE_PR_FETCH_TIMEOUT_S,
            include_comments=True,
        )
    except github_rest.GitHubRestTimeout as exc:
        raise subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=exc.timeout) from exc
    except github_rest.GitHubRestError as exc:
        raise RuntimeError(str(exc)) from exc
    if not isinstance(payload, list):
        raise RuntimeError("gh pr list returned a non-list payload")
    if getattr(payload, "truncated", False):
        raise RuntimeError("GitHub pull list truncated")

    now = datetime.now(UTC)
    rows = [row for item in payload if isinstance(item, dict) if (row := _eligible_idle_pr(item, now=now)) is not None]
    rows.sort(key=lambda row: (-row["minutes_idle"], row["number"]))
    return {"idle_prs": rows}


def _run_idle_pr_refresh(collector: Callable[[], Any], scope: str) -> None:
    try:
        value = collector()
        if not isinstance(value, dict) or not isinstance(value.get("idle_prs"), list):
            raise RuntimeError("idle PR collector returned an invalid payload")
    except subprocess.TimeoutExpired:
        _idle_pr_last_error[scope] = "gh_timeout"
        _idle_pr_next_retry_at[scope] = time.monotonic() + 30.0
        return
    except Exception:
        _idle_pr_last_error[scope] = "gh_unavailable"
        _idle_pr_next_retry_at[scope] = time.monotonic() + 30.0
        return

    generated_at = _isoformat_z(datetime.now(UTC))
    cache_set(f"{IDLE_PR_CACHE_KEY}{scope}", (value, generated_at))
    _idle_pr_last_good[scope] = (value, generated_at)
    _idle_pr_last_error.pop(scope, None)
    _idle_pr_next_retry_at[scope] = 0.0


def _schedule_idle_pr_refresh(collector: Callable[[], Any], scope: str) -> None:
    if time.monotonic() < _idle_pr_next_retry_at.get(scope, 0.0):
        return
    with _idle_pr_refresh_lock:
        thread = _idle_pr_refresh_threads.get(scope)
        if thread is not None and thread.is_alive():
            return
        worker = threading.Thread(
            target=_run_idle_pr_refresh,
            args=(collector, scope),
            daemon=True,
            name="orient-idle-pr-refresh",
        )
        _idle_pr_refresh_threads[scope] = worker
        worker.start()


def _cached_idle_pr_section(
    collector: Callable[..., Any],
    *,
    ctx: MonitorContext | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    ttl = ORIENT_SECTION_TTLS["idle_prs"]
    scope = _ctx_cache_scope(ctx)
    collector = functools.partial(collector, ctx)
    cached = cache_get(f"{IDLE_PR_CACHE_KEY}{scope}", ttl=ttl)
    if isinstance(cached, tuple) and len(cached) == 2:
        value, generated_at = cached
        if isinstance(value, dict) and isinstance(generated_at, str):
            return value, {
                "generated_at": generated_at,
                "stale_after_s": ttl,
                "source": "gh",
                "cache": "hit",
            }

    _schedule_idle_pr_refresh(collector, scope)
    scoped_last_good = _idle_pr_last_good.get(scope)
    if scoped_last_good is not None:
        value, generated_at = scoped_last_good
        meta: dict[str, Any] = {
            "generated_at": generated_at,
            "stale_after_s": ttl,
            "source": "gh",
            "cache": "miss",
            "stale": True,
            "refreshing": (_t := _idle_pr_refresh_threads.get(scope)) is not None and _t.is_alive(),
        }
        if (_err := _idle_pr_last_error.get(scope)) is not None:
            meta["error"] = _err
        return value, meta

    meta = {
        "generated_at": _isoformat_z(datetime.now(UTC)),
        "stale_after_s": ttl,
        "source": "gh",
        "cache": "miss",
        "refreshing": (_t := _idle_pr_refresh_threads.get(scope)) is not None and _t.is_alive(),
        "error": _idle_pr_last_error.get(scope, "refreshing"),
    }
    return None, meta


def reset_detached_orient_state_for_tests(ctx: MonitorContext | None = None) -> None:
    """Drain and clear the test context, defaulting to the shared app if built."""
    if ctx is None:
        shared_app = globals().get("app")
        if shared_app is None:
            return
        ctx = shared_app.state.ctx
    state = _orient_state(ctx)
    with state.lock:
        threads = [*state.refresh_threads.values(), *state.fresh_threads.values()]
    for thread in threads:
        thread.join(timeout=2.0)
    with state.lock:
        state.refresh_threads.clear()
        state.fresh_threads.clear()
        state.values.clear()
        state.errors.clear()
        state.pending.clear()
        state.demand.clear()
    _integrity_verdicts.clear()


def _collect_orient_section(
    collector: Callable[[], Any],
    key: str,
    state: _OrientState,
    *,
    collect_slot: bool,
    background: bool = False,
) -> None:
    """Collect one section and publish it by compare-and-set on when the collection began.

    A value replaces the stored value only if its stamp is newer, so the stored
    value never goes backwards; it clears an older error. An error is recorded
    only if it is newer than both the stored value and the last error. It never
    raises: a collector that raises anything, ``SystemExit`` included, records
    that error. A cheap section takes no permit, so it cannot queue behind
    a hung slow collector.
    """
    stamp, generated_at, value, error = _orient_clock(), _isoformat_z(datetime.now(UTC)), None, None
    try:
        with state.slots if collect_slot else nullcontext():
            stamp, generated_at = _orient_clock(), _isoformat_z(datetime.now(UTC))
            value = collector()
    except BaseException as exc:  # a worker thread would otherwise end without recording an error
        try:
            error = str(exc) or type(exc).__name__
        except BaseException:  # an exception whose text itself raises
            error = type(exc).__name__
    with state.lock:
        stored = state.values.get(key)
        last_error = state.errors.get(key)
        newer_than_value = stored is None or stamp > stored[2]
        newer_than_error = last_error is None or stamp > last_error[1]
        if error is None and newer_than_value:
            state.values[key] = (value, generated_at, stamp)
            if newer_than_error:
                state.errors.pop(key, None)
        elif error is not None and newer_than_value and newer_than_error:
            state.errors[key] = (error, stamp)
        if background:
            state.pending.discard(key)
        elif state.fresh_threads.get(key) is threading.current_thread():
            # A later fresh request must start a new scan once this one publishes.
            state.fresh_threads.pop(key)


def _schedule_detached_orient_refresh(
    key: str,
    collector: Callable[[], Any],
    state: _OrientState,
    *,
    collect_slot: bool,
    fresh: bool = False,
) -> threading.Thread:
    """Start the section's background (or ``fresh``) collection unless one is running; return the running one.

    Each kind is single-flight per section, so a section has at most one of each.
    """
    threads = state.fresh_threads if fresh else state.refresh_threads
    with state.lock:
        thread = threads.get(key)
        if thread is not None and thread.is_alive():
            return thread
        worker = threading.Thread(
            target=_collect_orient_section,
            args=(collector, key, state),
            kwargs={"collect_slot": collect_slot, "background": not fresh},
            daemon=True,
            name=f"orient-{key}-{'fresh' if fresh else 'refresh'}",
        )
        threads[key] = worker
        if not fresh:
            state.pending.add(key)
        worker.start()
        return worker


_THREAD_WAIT_POLL_S = 0.005


async def _await_thread(thread: threading.Thread, timeout: float) -> None:
    """Wait up to ``timeout`` for ``thread`` without holding a pool thread.

    A full orient waits on up to a dozen detached sections at once. Joining each
    on a pool thread filled the pool with waiters, so a cold read took twice
    the wait.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while thread.is_alive() and (remaining := deadline - loop.time()) > 0:
        await asyncio.sleep(min(_THREAD_WAIT_POLL_S, remaining))


async def _cached_detached_orient_section(
    key: str,
    collector: Callable[..., Any],
    *,
    ctx: MonitorContext | None = None,
    ttl: float | None = None,
    source: str | None = None,
    is_async: bool = False,
    fresh: bool = False,
    collect_slot: bool,
    wait_s: float | None = None,
) -> tuple[Any, dict]:
    """Serve one orient section from its stored value.

    A stored value younger than the TTL is a ``hit``. An older one, whatever
    its age and TTL (zero included), is served at once, labelled ``stale`` with
    its own ``generated_at``, while one background refresh runs. With no stored
    value the read waits up to ``wait_s`` for that refresh. ``fresh`` starts
    (or joins) the section's one fresh collection and waits up to ``wait_s``.
    Every collection publishes the same way (``_collect_orient_section``), and
    nothing cancels another. A value is current when it is younger than the TTL
    or its collection began during this read. A hung collector never pins the
    gather. A read without a stored value, or a failed fresh read, returns
    null with ``meta.error``.
    """
    ttl = ORIENT_SECTION_TTLS.get(key, 60.0) if ttl is None else ttl
    source = ORIENT_SECTION_SOURCES.get(key, "fs") if source is None else source
    wait_s = DETACHED_ORIENT_INLINE_WAIT_S if wait_s is None else wait_s

    def compute() -> Any:
        return asyncio.run(collector(ctx)) if is_async else collector(ctx)

    state = _orient_state(resolve_context(ctx))
    meta: dict[str, Any] = {"stale_after_s": ttl, "source": source, "cache": "miss"}
    read_started = _orient_clock()
    with state.lock:
        before = state.values.get(key)
    if not fresh and before is not None and read_started - before[2] < ttl:
        return before[0], {**meta, "generated_at": before[1], "cache": "hit"}

    worker = _schedule_detached_orient_refresh(key, compute, state, collect_slot=collect_slot, fresh=fresh)
    if fresh or before is None:
        await _await_thread(worker, wait_s)
    timeout = f"section_timeout_{wait_s}s" if fresh and worker.is_alive() else None

    with state.lock:
        stored = state.values.get(key)
        last_error = state.errors.get(key)
        error = timeout or (last_error[0] if last_error is not None else None)
        refreshing = key in state.pending
    # A concurrent background failure can coexist with a newly published
    # measurement, including one awaited by several fresh requests.
    fresh_unavailable = fresh and (timeout is not None or (error is not None and stored is before))
    if stored is not None and not fresh_unavailable:
        value, generated_at, stamp = stored
        current = stamp >= read_started or _orient_clock() - stamp < ttl
        meta.update(generated_at=generated_at, stale=not current, refreshing=refreshing)
        if error is not None:
            meta["error"] = error
        return value, meta

    meta.update(generated_at=_isoformat_z(datetime.now(UTC)), refreshing=refreshing, error=error or "refreshing")
    return None, meta


def _collect_runtime_orient_data(ctx: MonitorContext | None = None) -> dict:
    resolved = resolve_context(ctx)
    # One shared counter across every usage read this section makes — agent
    # inventory (last-used models), the day usage summary, today's outcome
    # counts, and the per-agent headroom scan — so corrupt usage evidence is
    # surfaced on the runtime section instead of discarded (#9924).
    unreadable: dict[str, int] = {"files": 0, "lines": 0, "records": 0}
    agents = runtime_api.list_runtime_agents(ctx=resolved, unreadable=unreadable)
    usage = runtime_api.summarize_runtime_usage(days=1, ctx=resolved, unreadable=unreadable)
    recent = runtime_api.runtime_recent_outcomes_today(ctx=resolved, unreadable=unreadable)
    headroom = {}
    for agent_info in agents:
        name = agent_info.get("name")
        model = agent_info.get("default_model")
        if not name or not model:
            continue
        try:
            ok, _ = has_headroom(str(name), str(model), unreadable=unreadable)
        except Exception:
            ok = False
        headroom[str(name)] = ok

    by_agent = usage.get("by_agent", {})

    return {
        "agents": [agent["name"] for agent in agents if agent.get("name")],
        "recent_outcomes": recent,
        "by_agent": by_agent,
        "headroom": headroom,
        "unreadable": runtime_api._unreadable_summary(unreadable),
    }


def _collect_delegate_orient_data(ctx: MonitorContext | None = None) -> dict:
    resolved_ctx = resolve_context(ctx)
    recent = delegate_api.list_delegate_tasks(status="all", limit=5, ctx=resolved_ctx)
    active = delegate_api.active_delegate_count(ctx=resolved_ctx)
    return {
        "active_count": active,
        "recent": recent["tasks"],
    }


def _collect_capacity_orient_data(ctx: MonitorContext | None = None) -> dict:
    resolved_ctx = resolve_context(ctx)
    budget = state_api.compute_routing_budget(
        budget_config_path=machinery_path("config/agent_budgets.yaml"),
        tasks_dir=resolved_ctx.roots.batch_state_dir / "tasks",
        project_root=resolved_ctx.roots.project_root,
        curriculum_root=resolved_ctx.roots.curriculum_root,
        batch_state_dir=resolved_ctx.roots.batch_state_dir,
    )
    lanes_summary = {}
    agents = budget.get("agents", {})
    in_flight = budget.get("in_flight", {})
    for lane in state_api.SUBSCRIPTION_LANES:
        data = agents.get(lane, {})
        health = data.get("health", {})
        healthy = health.get("healthy", True) if isinstance(health, dict) else True
        lanes_summary[lane] = {
            "in_flight": in_flight.get(lane, 0),
            "healthy": healthy,
            "burn_pct_7d": data.get("burn_pct_7d"),
            "remaining_pct": data.get("remaining_pct"),
            "status": data.get("status", "unknown"),
        }
    rec = budget.get("recommendation", {}).get("primary_agent_for_code")
    return {
        "lanes": lanes_summary,
        "primary_recommendation": rec,
    }


def _collect_rollovers_orient_data(ctx: MonitorContext | None = None) -> dict:
    resolved_ctx = resolve_context(ctx)
    return collect_rollover_orient_data(live_repo_root=resolved_ctx.roots.state_root)


logger = logging.getLogger(__name__)


def _port_open(host: str, port: int, timeout_s: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _core_bare_canary(ctx: MonitorContext | None = None) -> bool:
    """Detection canary for issue #2842: ``core.bare`` must stay false.

    ``core.bare`` lives in the SHARED ``.git/config``; if any subprocess flips it
    to ``true`` it silently breaks ``git`` work-tree ops for the main checkout AND
    every linked worktree at once. Reading/writing git config does not need a work
    tree, so this check still functions while the footgun is active — which is when
    it's needed. On drift it auto-resets to false and logs an alert. Never raises:
    the canary must not break health collection.
    """
    resolved_ctx = resolve_context(ctx)
    from lu_machinery.hygiene.check_core_bare import check_core_bare  # noqa: PLC0415 — script-path fallback
    try:
        ok, message = check_core_bare(resolved_ctx.roots.project_root, fix=True)
    except Exception:
        logger.exception("core.bare canary (#2842) failed to run")
        return True  # fail-open: don't raise a false alarm on canary error
    if "reset" in message or not ok:
        logger.warning("core.bare canary (#2842): %s", message)
    return ok


def _self_symlink_canary(ctx: MonitorContext | None = None) -> bool:
    """Detection canary for the node_modules ELOOP footgun.

    A self-referential ``node_modules`` symlink (``X -> X``) is an infinite
    loop. ``npm run <script>`` builds its child PATH by walking the directory
    tree upward and prepending every ancestor ``node_modules/.bin``; resolving
    the looping ancestor makes ``spawn`` return ``ELOOP``, so every npm build
    dies instantly with exit 194 and no output — looking like "Astro is broken"
    when it is not. The loop is gitignored so CI cannot catch it; only a local
    canary can. On detection it reports the link without removing it; repair is
    an explicit doctor action. Never raises: the canary must not break health
    collection. See the autopsy
    ``docs/bug-autopsies/node-modules-eloop-symlink.md``.
    """
    resolved_ctx = resolve_context(ctx)
    from lu_machinery.hygiene.check_self_symlinks import check_self_symlinks  # noqa: PLC0415 — script-path fallback
    try:
        ok, message = check_self_symlinks(resolved_ctx.roots.project_root, fix=False)
    except Exception:
        logger.exception("node_modules ELOOP canary failed to run")
        return True  # fail-open: don't raise a false alarm on canary error
    if not ok:
        logger.warning("node_modules ELOOP canary: %s", message)
    return ok


def _primary_integrity_canary(ctx: MonitorContext | None = None) -> bool:
    """Read-only checkout diagnostic for primary-checkout drift.

    #5803 follow-up: a worker detached the primary checkout (`checkout: moving
    from main to FETCH_HEAD`). Git-layer prevention was proven impossible
    against a same-UID process (design panel: gpt-5.6-sol + agy), so this
    canary detects drift and records diagnostic evidence without switching,
    fetching, or pulling the human's checkout.
    Never raises: the canary must not break health collection.
    """
    resolved_ctx = resolve_context(ctx)
    from lu_machinery.hygiene.check_primary_integrity import (  # noqa: PLC0415 — script-path fallback
        check_primary_integrity,
    )
    try:
        ok, message = check_primary_integrity(
            resolved_ctx.roots.project_root,
            fix=False,
            tasks_dir=resolved_ctx.roots.batch_state_dir / "tasks",
        )
    except Exception:
        logger.exception("primary-integrity canary failed to run")
        return True  # fail-open: don't raise a false alarm on canary error
    if not ok:
        logger.warning("primary-integrity canary: %s", message)
    return ok


def _node_modules_integrity_canary(ctx: MonitorContext | None = None) -> bool:
    """Read-only diagnostic for symlink corruption of the primary's node_modules.

    #6818 follow-up: sharing the primary's `node_modules`/`site/node_modules`
    with dispatch worktrees lets worktree writes reach the primary's real
    files (#6805 incident). ALERT-only, same posture as
    `_primary_integrity_canary` — detects and records, never repairs. Never
    raises: the canary must not break health collection.
    """
    resolved_ctx = resolve_context(ctx)
    from lu_machinery.hygiene.check_node_modules_integrity import (  # noqa: PLC0415 — script-path fallback
        check_node_modules_integrity,
    )
    try:
        ok, message = check_node_modules_integrity(
            resolved_ctx.roots.project_root,
            tasks_dir=resolved_ctx.roots.batch_state_dir / "tasks",
        )
    except Exception:
        logger.exception("node_modules-integrity canary failed to run")
        return True  # fail-open: don't raise a false alarm on canary error
    if not ok:
        logger.warning("node_modules-integrity canary: %s", message)
    return ok


def _venv_integrity_canary(ctx: MonitorContext | None = None) -> bool:
    """Read-only diagnostic for an empty/broken primary venv.

    #6830 follow-up: a mid-session venv rebuild left `.venv` with no
    site-packages, and separately, console-script launchers
    (`pytest`/`py.test`/`cbor2`) were found pointing at a deleted worktree
    venv. ALERT-only, same posture as `_node_modules_integrity_canary` —
    detects and records, never repairs. Never raises: the canary must not
    break health collection.
    """
    resolved_ctx = resolve_context(ctx)
    from lu_machinery.hygiene.check_venv_integrity import (  # noqa: PLC0415 — script-path fallback
        check_venv_integrity,
    )
    try:
        ok, message = check_venv_integrity(
            resolved_ctx.roots.project_root,
            tasks_dir=resolved_ctx.roots.batch_state_dir / "tasks",
        )
    except Exception:
        logger.exception("venv-integrity canary failed to run")
        return True  # fail-open: don't raise a false alarm on canary error
    if not ok:
        logger.warning("venv-integrity canary: %s", message)
    return ok


def _worktree_cleanup_integrity_canary(ctx: MonitorContext | None = None) -> bool:
    """Read-only diagnostic for a dark/red worktree-cleanup LaunchAgent.

    #6937 follow-up: launchd stopped starting the job after a venv rewrite
    (LWCR init failure, exit 78) and no receipt landed for two days.
    ALERT-only, same posture as `_venv_integrity_canary` — detects and
    records, never reloads launchd. Never raises: the canary must not break
    health collection.
    """
    resolved_ctx = resolve_context(ctx)
    from lu_machinery.hygiene.check_worktree_cleanup_integrity import (  # noqa: PLC0415 — script-path fallback
        check_worktree_cleanup_integrity,
    )
    try:
        ok, message = check_worktree_cleanup_integrity(
            resolved_ctx.roots.project_root,
            tasks_dir=resolved_ctx.roots.batch_state_dir / "tasks",
        )
    except Exception:
        logger.exception("worktree-cleanup-integrity canary failed to run")
        return True  # fail-open: don't raise a false alarm on canary error
    if not ok:
        logger.warning("worktree-cleanup-integrity canary: %s", message)
    return ok


def _tmp_usability_canary() -> dict:
    """Detection probe for issue #7164: /tmp usage plus a small write probe.

    A quota-exhausted tmpfs fails every write with EDQUOT while ``df`` still
    looks healthy; the probe classifies EDQUOT distinctly so the state is
    visible on the health glance instead of masquerading as random crashes.
    Never raises: the canary must not break health collection.
    """
    from lu_machinery.hygiene.check_tmp_usability import probe_tmp_usability  # noqa: PLC0415 — script-path fallback
    try:
        return probe_tmp_usability()
    except Exception:
        logger.exception("tmp-usability canary (#7164) failed to run")
        # fail-open: don't raise a false alarm on canary error
        return {"ok": True, "writable": True, "error": None, "probe_error": True}


def _backup_last_run_health(batch_state_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    receipt = batch_state_dir / "backups" / "last-run.json"
    try:
        data = json_depth.loads(receipt.read_text(encoding="utf-8"), max_depth=512)
    except FileNotFoundError:
        return {"status": "unknown", "reason": "no_receipt"}
    except (OSError, ValueError):
        return {"status": "unknown", "reason": "unreadable_receipt"}
    if not isinstance(data, dict) or type(data.get("exit_status")) is not int:
        return {"status": "unknown", "reason": "invalid_receipt"}
    finished = data.get("finished_at_utc")
    if not isinstance(finished, str):
        return {"status": "unknown", "reason": "invalid_receipt"}
    try:
        finished_at = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return {"status": "unknown", "reason": "invalid_receipt"}
    if finished_at.tzinfo is None:
        return {"status": "unknown", "reason": "invalid_receipt"}
    current = now or datetime.now(UTC)
    status = "failed" if data["exit_status"] != 0 else "ok"
    if status == "ok" and current - finished_at > timedelta(hours=26):
        status = "stale"
    return {
        "status": status,
        "exit_status": data["exit_status"],
        "finished_at_utc": finished,
    }


# The filesystem integrity canaries detect slow-changing host damage (a broken
# venv, a looping symlink). One pass of them costs seconds of CPU: while it
# alerts, the venv canary parses every task record (about 14 s for 15,000) and
# starts a cold import probe. Each health refresh reuses a verdict this young.
INTEGRITY_CANARY_TTL_S = 300.0
_integrity_verdicts: dict[str, tuple[bool, float]] = {}


def _integrity_verdict(name: str, canary: Callable[..., bool], ctx: MonitorContext) -> bool:
    key = f"{name}{_ctx_cache_scope(ctx)}"
    cached = _integrity_verdicts.get(key)
    now = _orient_clock()
    if cached is not None and now - cached[1] < INTEGRITY_CANARY_TTL_S:
        return cached[0]
    ok = canary(ctx=ctx)
    _integrity_verdicts[key] = (ok, now)
    return ok


def _collect_health_orient_data(
    ctx: MonitorContext | None = None,
    product_health: Callable[[MonitorContext], Mapping[str, Any]] | None = None,
) -> dict:
    resolved_ctx = resolve_context(ctx)
    mcp_sources_ok = _port_open("127.0.0.1", 8766, 0.2)
    tmp_usability = _tmp_usability_canary()
    core_bare_ok = _core_bare_canary(ctx=resolved_ctx)
    self_symlink_ok = _integrity_verdict("self_symlink", _self_symlink_canary, resolved_ctx)
    primary_integrity_ok = _integrity_verdict("primary", _primary_integrity_canary, resolved_ctx)
    node_modules_integrity_ok = _integrity_verdict("node_modules", _node_modules_integrity_canary, resolved_ctx)
    venv_integrity_ok = _integrity_verdict("venv", _venv_integrity_canary, resolved_ctx)
    worktree_cleanup_integrity_ok = _integrity_verdict("worktree_cleanup", _worktree_cleanup_integrity_canary, resolved_ctx)
    fields: dict[str, Any] = {
        "api": True,
        "mcp_sources": mcp_sources_ok,
        "mcp_rag": mcp_sources_ok,
    }
    if product_health is not None:
        fields.update(product_health(resolved_ctx))
    return {
        **fields,
        "git_core_bare_ok": core_bare_ok,
        "node_modules_symlinks_ok": self_symlink_ok,
        "primary_integrity_ok": primary_integrity_ok,
        "node_modules_integrity_ok": node_modules_integrity_ok,
        "venv_integrity_ok": venv_integrity_ok,
        "worktree_cleanup_integrity_ok": worktree_cleanup_integrity_ok,
        "tmp_usability_ok": bool(tmp_usability.get("ok")),
        "tmp_usability": tmp_usability,
        "backup_last_run": _backup_last_run_health(resolved_ctx.roots.batch_state_dir),
    }


def _first_non_empty_line(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    except OSError:
        return ""
    return ""


def _collect_session_hints_orient_data(ctx: MonitorContext | None = None) -> list[dict]:
    resolved_ctx = resolve_context(ctx)
    session_state_dir = resolved_ctx.roots.project_root / "docs" / "session-state"
    if not session_state_dir.exists():
        return []
    hints = []
    for path in sorted(session_state_dir.glob("*.md"), reverse=True)[:10]:
        hints.append(
            {
                "file": str(path.relative_to(resolved_ctx.roots.project_root)),
                "first_line": _first_non_empty_line(path),
            }
        )
    return hints


# ==================== SHARED ENDPOINTS ====================


def _health_instance_identity(ctx: MonitorContext | None = None) -> dict[str, str | None]:
    """Return loopback-safe opaque host id and serving vs checkout SHAs for /api/health."""
    resolved_ctx = resolve_context(ctx)
    host_label = resolve_launcher_host_id()
    head_proc = _run_command(["git", "--no-optional-locks", "rev-parse", "HEAD"], ctx=ctx)
    checkout_sha = head_proc.stdout.strip() if head_proc.returncode == 0 else None
    project_root = resolved_ctx.roots.project_root.resolve()
    if is_release_root(project_root):
        serving_sha = project_root.name
        serving_mode = "release"
    else:
        serving_sha = None
        serving_mode = "checkout"
    return {
        "host": host_label,
        "git_sha": checkout_sha,
        "checkout_sha": checkout_sha,
        "serving_sha": serving_sha,
        "serving_mode": serving_mode,
    }


@core_router.get("/api", status_code=307)
async def api_index():
    """API root — redirect humans to the interactive docs explorer (#7090)."""
    return RedirectResponse(url="/docs", status_code=307)


@core_router.get("/api/health")
def health_check(request: Request, ctx: MonitorContext = Depends(get_ctx)):
    """Root health check — returns server status, API and lu-machinery versions, uptime."""
    now = datetime.now(UTC)
    uptime = now - _SERVER_START
    return {
        "status": "ok",
        "version": request.app.version,
        "machinery_version": _machinery_version(),
        "uptime_seconds": int(uptime.total_seconds()),
        "started_at": _SERVER_START.isoformat(),
        "checked_at": now.isoformat(),
        "instance": _health_instance_identity(ctx),
        "resilience": get_resilience_snapshot(),
        "codexbar": scheduler_status(),
    }


def _ctx_cache_scope(ctx: MonitorContext | None) -> str:
    """Project-root scope for the idle PR feed and integrity probe caches."""
    return ctx_cache_scope(ctx)


async def _cached_orient_section(
    key: str,
    collector: Callable[..., Any] | Callable[..., Awaitable[Any]],
    *,
    is_async: bool = False,
    ctx: MonitorContext | None = None,
    ttl: float | None = None,
    source: str | None = None,
    background: bool = False,
    fresh: bool = False,
) -> tuple[Any, dict]:
    """Run one orient collector through the section cache (``_cached_detached_orient_section``).

    Returns (value, meta). Meta always includes ``generated_at``,
    ``stale_after_s``, ``source``, and ``cache`` ("hit" / "miss"); it
    adds ``error`` when a collector fails. Unavailable values use null.

    Detached sections (``DETACHED_ORIENT_SECTION_KEYS`` and product sections)
    wait at most ``DETACHED_ORIENT_INLINE_WAIT_S`` and share the collect slots.
    The others (git, runtime, session_hints) wait up to
    ``ORIENT_SECTION_HARD_TIMEOUT_S`` for their first or ``fresh`` value, as
    their inline collection did, and keep off the slots.

    ``ttl`` and ``source`` come from a product section; a machinery section
    takes them from ``ORIENT_SECTION_TTLS`` and ``ORIENT_SECTION_SOURCES``.
    """
    if key == "idle_prs":
        return _cached_idle_pr_section(collector, ctx=ctx)
    detached = background or key in DETACHED_ORIENT_SECTION_KEYS
    return await _cached_detached_orient_section(
        key, collector, ctx=ctx, ttl=ttl, source=source, is_async=is_async, fresh=fresh,
        collect_slot=detached, wait_s=DETACHED_ORIENT_INLINE_WAIT_S if detached else ORIENT_SECTION_HARD_TIMEOUT_S,
    )


@dataclass(frozen=True)
class OrientLayout:
    """The orient sections of one app: machinery sections plus the product's, in response order."""

    keys: tuple[str, ...]
    lean: tuple[str, ...]
    specs: Mapping[str, tuple[Callable[..., Any], bool]]
    product: Mapping[str, OrientSection]


def _orient_layout(product: ProductContribution) -> OrientLayout:
    """Compose the machinery sections with the product's at their ``after`` anchors."""
    specs = _orient_section_specs(product)
    sections = dict(product.orient_sections())
    keys = list(ORIENT_SECTION_KEYS)
    pending = dict(sections)
    while pending:
        placed = [key for key, section in pending.items() if section.after in keys]
        if not placed:
            raise RuntimeError(f"orient sections with unknown anchors: {sorted(pending)}")
        for key in placed:
            section = pending.pop(key)
            keys.insert(keys.index(section.after) + 1, key)
            specs[key] = (section.collector, section.is_async)
    lean_keys = set(LEAN_ORIENT_SECTIONS) | {key for key, section in sections.items() if section.lean}
    lean = tuple(key for key in keys if key in lean_keys)
    return OrientLayout(tuple(keys), lean, specs, sections)


def _parse_orient_sections(sections_param: str | None, *, lean: bool = False, layout: OrientLayout) -> list[str]:
    """Validate and expand the optional ``sections`` query param.

    An explicit ``sections`` list always wins. When it is absent, ``lean`` selects the
    lightweight cold-start preset (``layout.lean``); otherwise the full payload.
    """
    default = list(layout.lean if lean else layout.keys)
    if sections_param is None:
        return default
    keys = [part.strip() for part in sections_param.split(",") if part.strip()]
    if not keys:
        return default
    unknown = [key for key in keys if key not in layout.specs]
    if unknown:
        valid = ", ".join(layout.keys)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown orient section(s): {', '.join(unknown)}. Valid keys: {valid}",
        )
    return keys


def _orient_section_specs(product: ProductContribution) -> dict[str, tuple[Callable[..., Any], bool]]:
    """Return machinery orient collector specs using live module attributes (test-friendly)."""
    health = (
        _collect_health_orient_data
        if product.health is None
        else functools.partial(_collect_health_orient_data, product_health=product.health)
    )
    return {
        "git": (_collect_git_orient_data, False),
        "issues": (_collect_issues_orient_data, False),
        "idle_prs": (_collect_idle_prs_orient_data, False),
        "runtime": (_collect_runtime_orient_data, False),
        "delegate": (_collect_delegate_orient_data, False),
        "capacity": (_collect_capacity_orient_data, False),
        "rollovers": (_collect_rollovers_orient_data, False),
        "health": (health, False),
        "session_hints": (_collect_session_hints_orient_data, False),
    }


async def _collect_orient_sections(
    ctx: MonitorContext, layout: OrientLayout, selected: Sequence[str], *, fresh: bool = False,
) -> list[tuple[Any, dict]]:
    """Requests and the lifespan warmer share the same section collectors."""
    return await asyncio.gather(*(
        _cached_orient_section(
            key, layout.specs[key][0], is_async=layout.specs[key][1], ctx=ctx,
            ttl=layout.product[key].ttl_s if key in layout.product else None,
            source=layout.product[key].source if key in layout.product else None,
            background=key in layout.product, fresh=fresh,
        )
        for key in selected
    ))


async def refresh_orient_periodically(ctx: MonitorContext, product: ProductContribution) -> None:
    """Keep the sections agents are reading warm between their polls, without stacking TTLs."""
    startup = True
    state = _orient_state(ctx)
    while True:
        try:
            layout = _orient_layout(product)
            # Warm expensive product scans once at startup. Afterwards a section
            # is refreshed only while requests read it: an app nobody orients
            # against runs no task-store scans, GitHub reads or host probes.
            selected = [
                key for key in layout.keys
                if (startup and key in layout.product) or ((demand := state.demand.get(key)) and demand.active())
            ]
            await _collect_orient_sections(ctx, layout, selected)
        except Exception:
            logger.warning("Orient refresh failed", exc_info=True)
        startup = False
        await asyncio.sleep(ORIENT_REFRESH_INTERVAL_S)


def _cap_lean_orient(response: dict[str, Any]) -> dict[str, Any]:
    """Keep the lean preset inside the orientation byte bound.

    Sections and section metadata that do not fit become locators.
    ``generated_at`` stays. The full ``/api/orient`` payload is unchanged.
    """
    protected = {"generated_at", "meta", "_telemetry", "orientation_bound"}

    def size(body: dict[str, Any]) -> int:
        return len(json_bytes(body))

    if size(response) <= PACKET_MAX_BYTES:
        return response
    omitted: list[str] = []
    bound: dict[str, Any] = {
        "max_bytes": PACKET_MAX_BYTES,
        "complete": False,
        "omitted": {"count": 0, "locators": omitted},
    }
    response["orientation_bound"] = bound
    candidates = [(response, key) for key in response if key not in protected]
    meta = response.get("meta", {})
    candidates.extend((meta, key) for key in meta)
    while size(response) > PACKET_MAX_BYTES:
        if not candidates:
            break
        candidate = max(
            candidates,
            key=lambda item: len(json_bytes({item[1]: item[0][item[1]]})),
        )
        candidates.remove(candidate)
        container, key = candidate
        locator = f"GET /api/orient?sections={key}"
        section = container[key]
        stub: dict[str, Any] = {
            "omitted": True,
            "locator": locator,
            "bytes": len(json_bytes({key: section})),
        }
        if isinstance(section, dict):
            if isinstance(section.get("generated_at"), str):
                stub["generated_at"] = section["generated_at"]
            counts = section.get("counts")
            if isinstance(counts, dict) and len(json_bytes(counts)) <= 400:
                stub["counts"] = counts
        container[key] = stub
        if locator not in omitted:
            omitted.append(locator)
        bound["omitted"]["count"] = len(omitted)
    if size(response) > PACKET_MAX_BYTES:
        response.pop("orientation_bound", None)
        for key, value in list(response.items()):
            if key in {"generated_at", "meta", "_telemetry"} or not isinstance(value, dict):
                continue
            value.pop("counts", None)
    return response


@core_router.get("/api/orient")
async def orient(
    request: Request,
    fresh: bool = False,
    lean: bool = Query(
        False,
        description="Lean cold-start preset: return only the lightweight sections "
        "(git, runtime, delegate, rollovers, governance, health, session_hints), "
        "skipping the heavy pipeline/issues/wiki and idle_prs. Ignored when 'sections' is given.",
    ),
    sections: str | None = Query(
        None,
        description="Comma-separated subset of orient sections to collect.",
    ),
    role: str | None = Query(
        None,
        max_length=reg.MAX_QUERY_VALUE_LEN,
        description="Opt-in ADR-011 P3 cold-start research role. Adds pointer-only research.",
    ),
):
    """One-shot agent orientation.

    A requested section can be null when collection is pending, fails, or
    times out. See ``meta[section]["error"]`` for the reason.

    Query params:
        fresh: if true, invalidate every ``orient_*`` cache entry before
            gathering. Use it when an agent just committed, renamed a
            file, or otherwise needs to see a change it made moments
            ago without waiting for the longest section TTL (up to
            120 s for ``issues``/``wiki``). The idle-PR refresh remains
            cache-first and detached. Reviewer BLOCKER B3 / #1309.
        sections: comma-separated list of section keys to collect. Unknown
            keys return 400. Omitted = full payload (back-compat).
        role: ADR-011 P3 opt-in. Absent → the response is byte-identical to
            the pre-P3 orient (no research key, no shared-cache contamination).
            Present + registry enabled → a pointer-only ``research`` section of
            the role's ``cold_start_roles`` announcements (≤5 / ≤1.5 KB, bodies
            fetched on demand). Computed fresh per request and never stored in
            the shared ``orient_*`` cache, so two roles can never contaminate
            each other's pointers.
    """
    if fresh:
        cache_invalidate("orient_")

    layout = _orient_layout(request.app.state.product)
    selected = _parse_orient_sections(sections, lean=lean, layout=layout)
    # #7494: thread THIS app's context into every collector — the zero-arg
    # invocation made all 14 collectors fall back to production_context(),
    # defeating create_app() isolation (and rebuilding contexts per call).
    request_ctx = get_ctx(request)
    state = _orient_state(request_ctx)
    for key in selected:
        state.demand.setdefault(key, Demand()).touch()
    gather_results = await _collect_orient_sections(request_ctx, layout, selected, fresh=fresh)

    section_data: dict[str, Any] = {}
    section_metas: dict[str, dict[str, Any]] = {}
    for key, (value, meta) in zip(selected, gather_results, strict=True):
        section_data[key] = value
        section_metas[key] = meta

    generated_candidates: list[str] = [
        ts for m in section_metas.values() if isinstance(ts := m.get("generated_at"), str)
    ]
    top_generated_at = min(generated_candidates) if generated_candidates else _isoformat_z(datetime.now(UTC))

    response: dict[str, Any] = {
        "generated_at": top_generated_at,
        "meta": section_metas,
    }

    for key in layout.keys:
        if key not in section_data:
            continue
        value = section_data[key]
        response[key] = value
        if key in ("issues", "idle_prs"):
            response[key] = value.get(key) if isinstance(value, dict) else None
            if error := section_metas[key].get("error"):
                response[f"{key}_error"] = error
        product_section = layout.product.get(key)
        if product_section is not None and product_section.merge_meta and isinstance(value, Mapping):
            section_metas[key] = {**section_metas[key], **value}

    await asyncio.to_thread(_attach_cold_start_research, response, role)
    body = await asyncio.to_thread(add_json_telemetry, response, session_id=session_id_from_request(request))
    if lean and not (sections and sections.strip()):
        body = _cap_lean_orient(body)
    return body


def _attach_cold_start_research(response: dict[str, Any], role: str | None) -> None:
    """Add the ADR-011 P3 pointer-only research section for an opt-in role.

    No-op (leaving the response byte-identical to the pre-P3 orient) when no role
    is given, the kill switch is off, the registry cannot be exposed, or anything
    in the selector/loader path raises unexpectedly — fail-open, never a 500 for
    orient as a whole. POINTERS ONLY: it calls the role-only cold-start selector
    (``cold_start_roles``, never the AND matcher), never ``select_bodies``; record
    bodies are fetched on demand from the documented, well-known
    ``GET /api/knowledge/record/{id}`` (see ``docs/MONITOR-API.md``) — omitted here
    rather than repeated per response so the envelope stays inside the same
    ``MAX_FILTERED_BYTES`` (1536 B) budget the selector already caps pointers to.
    Computed inline, never cached at the orient layer, so role-specific pointers
    never share a cache key.
    """
    if not role or not role.strip():
        return
    try:
        if not reg.is_enabled():
            return
        runtime = reg.load_runtime_safe()
        if runtime is None:
            return
        pointers, _dropped = reg.select_cold_start_pointers(runtime, role)
        response["research"] = {"enabled": True, "records": pointers}
    except Exception:
        # Do not interpolate the role string — it is request-derived and would
        # reintroduce py/log-injection even after control-char scrubbing.
        logger.warning(
            "orient: cold-start research selector failed unexpectedly; omitting research section",
            exc_info=True,
        )
        response.pop("research", None)


# ==================== STATIC FILES (MUST BE LAST) ====================


MACHINERY_HOME_PATHS = frozenset({"machinery", "machinery/"})
_VERSION_PLACEHOLDER = "__MACHINERY_VERSION__"
_MACHINERY_REPOSITORY_PLACEHOLDER = "__MACHINERY_REPOSITORY__"
_NAV_PLACEHOLDER = "__MACHINERY_NAV__"
# URL Standard: characters trimmed from both ends of a URL, and the spellings of "." and ".." segments.
_URL_TRIM = "".join(map(chr, range(0x21)))
_SINGLE_DOT_SEGMENTS = frozenset({".", "%2e"})
_DOUBLE_DOT_SEGMENTS = frozenset({"..", ".%2e", "%2e.", "%2e%2e"})


def _dashboard_file(dashboards_dir: Path, path: str, product: Any) -> Path | None:
    """The machinery dashboard file the static route serves at ``path``, or ``None`` when it serves none.

    ``None`` leaves the path to the product's declared files. This is the one lookup: the
    route serves what it returns, and the navigation uses it to tell machinery pages from
    product pages. Raises ``HTTPException(403)`` for a path that escapes the dashboards.
    """
    if path in MACHINERY_HOME_PATHS:
        # The machinery home has its own address, so a product that owns ``/`` never hides it.
        return dashboards_dir / "index.html"
    if not path or path in {"/", "index.html"}:
        return None if product_file(product, "index.html") is not None else dashboards_dir / "index.html"
    file_path = _safe_join(dashboards_dir, path)
    if file_path is None:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    dashboards_root = dashboards_dir.resolve()
    # Keep explicit traversal guard if relative path join bypassed via symlink tricks.
    if not file_path.resolve().is_relative_to(dashboards_root):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if file_path.is_file():
        return file_path
    if file_path.is_dir() and (file_path / "index.html").is_file():
        return file_path / "index.html"
    if not file_path.suffix:
        html_candidate = _safe_join(dashboards_dir, f"{path}.html")
        if (
            html_candidate is not None
            and html_candidate.resolve().is_relative_to(dashboards_root)
            and html_candidate.is_file()
        ):
            return html_candidate
    return None


def _request_path(url: str) -> str | None:
    """The path the static route receives when a browser follows a navigation link to ``url``, or ``None``.

    The browser first parses the rendered ``href`` attribute (HTML
    Standard: a NUL becomes U+FFFD; the rest is escaped by ``html.escape``), then the URL
    (URL Standard, for a link on a same-site page): C0 control and space characters are
    trimmed from both ends and tab and newline removed; the fragment and query are split
    off; a backslash is a slash; ``.`` and ``..`` segments, plain or percent-encoded, are
    resolved. The server then percent-decodes the path, as uvicorn does. ``None`` means
    the link does not name a fixed path on this site: it names another host (``//host``)
    or a scheme, is relative to the current page, or is not text a page can carry.
    """
    try:
        url.encode("utf-8")
    except UnicodeEncodeError:
        return None
    url = url.replace("\x00", "\ufffd").strip(_URL_TRIM).translate({0x09: None, 0x0A: None, 0x0D: None})
    url = url.partition("#")[0].partition("?")[0]
    url = url.replace("\\", "/")
    if not url.startswith("/") or url.startswith("//"):
        return None
    segments: list[str] = []
    parts = url[1:].split("/")
    for index, segment in enumerate(parts):
        last = index == len(parts) - 1
        if segment.lower() in _DOUBLE_DOT_SEGMENTS:
            if segments:
                segments.pop()
            if last:
                segments.append("")
        elif segment.lower() in _SINGLE_DOT_SEGMENTS:
            if last:
                segments.append("")
        else:
            segments.append(segment)
    return unquote("/".join(segments))


def _leaves_to_product(url: str, dashboards_dir: Path, product: Any) -> bool:
    """Whether the static route leaves the path a link to the declared ``url`` opens to the product.

    False when the link does not name a fixed path on this site (``_request_path``), or the
    route's own lookup answers the path with a machinery dashboard file or refuses it, or
    the lookup fails on the filesystem (on Python 3.12 a symlink loop raises
    ``RuntimeError``; an overlong name raises ``OSError``). The failure then drops this one
    link, never the page. The route's next lookup, the product's declared file, runs here
    too, so a declaration the route cannot read raises to the caller instead of being
    listed; no declared file is fine, since a product router may serve the path.
    """
    path = _request_path(url)
    if path is None:
        return False
    try:
        if _dashboard_file(dashboards_dir, path, product) is not None:
            return False
        product_file(product, "index.html" if path in {"", "/"} else path)
        return True
    except (HTTPException, OSError, RuntimeError, ValueError):
        return False


def _machinery_nav_html(current_file: str | None, dashboards_dir: Path, product: Any) -> str:
    """The shared Monitor navigation: every machinery page once, then the pages the product declares.

    The current page is the machinery page whose file is being served. A product page is
    listed only because the product declares it, so no page is named here that the
    installed product does not serve. A declared URL is skipped when the static route does
    not leave its path to the product (``_leaves_to_product``): a product contract that
    reuses a machinery URL replaces the contract, never the page. A URL declared twice is
    listed once; distinct declared URLs are never merged, since the product may tell them
    apart. The product's declarations are its own code, checked only as a whole when it is
    loaded. Each one is read here only as a ``PageContract`` whose URL, file and label are
    plain strings; any other value, or a declaration whose lookup fails (a malformed static
    file entry), drops its own link with a logged reason, never the page.
    """

    def link(page: PageContract, attrs: str) -> str:
        return f'<a href="{html.escape(page.url, quote=True)}"{attrs}>{html.escape(page.nav_label)}</a>'

    links = [link(page, ' aria-current="page"' if page.file == current_file else "") for page in PAGE_CONTRACTS]
    try:
        declared = tuple(getattr(product, "page_contracts", ()))
    except Exception as exc:
        logger.warning("Monitor navigation skips the product page list: %s", type(exc).__name__)
        declared = ()
    listed: set[str] = set()
    for index, page in enumerate(declared):
        if type(page) is not PageContract or any(type(value) is not str for value in (page.url, page.file, page.label)):
            logger.warning("Monitor navigation skips product page %d: not a page contract of plain strings", index)
            continue
        try:
            if page.url in listed or not _leaves_to_product(page.url, dashboards_dir, product):
                continue
            product_link = link(page, ' class="product"' if listed else ' class="product first"')
            listed.add(page.url)
        except Exception as exc:
            logger.warning("Monitor navigation skips product page %d: %s", index, type(exc).__name__)
            continue
        links.append(product_link)
    return "".join(links)


def _machinery_page_response(page: Path, dashboards_dir: Path, product: Any) -> HTMLResponse:
    """A machinery page with the installed release, its repository and the shared navigation filled in.

    The page reads the version from the document, not from ``/api/health``, so a
    viewer behind the UI gateway (which does not admit that route) still sees it.
    With no distribution installed the value is empty and the header says unknown.
    """
    text = page.read_text(encoding="utf-8")
    if _VERSION_PLACEHOLDER in text or _MACHINERY_REPOSITORY_PLACEHOLDER in text:
        version = html.escape(_machinery_version() or "", quote=True)
        try:
            repository = load_fleet_repos()["machinery"].github
        except (FleetRepoError, KeyError):
            repository = ""
        text = text.replace(_VERSION_PLACEHOLDER, version)
        text = text.replace(_MACHINERY_REPOSITORY_PLACEHOLDER, html.escape(repository, quote=True))
    if _NAV_PLACEHOLDER in text:
        text = text.replace(_NAV_PLACEHOLDER, _machinery_nav_html(page.name, dashboards_dir, product))
    return HTMLResponse(text)


def _safe_join(base: Path, *parts: str | Path) -> Path | None:
    try:
        return safe_join(base, *parts)
    except ValueError:
        return None


@core_router.get("/{path:path}")
def serve_static(path: str, request: Request, ctx: MonitorContext = Depends(get_ctx)):
    """Serve a machinery dashboard file, else a file the product declares (C-4687).

    Machinery files come from ``ctx.roots.dashboards_dir`` with traversal and
    symlink confinement. A product page or asset is served only when the
    product contribution declares its URL path; ``/`` is the product home
    when the product declares one, else the machinery home, which is always
    at ``/machinery``.
    """
    dashboards_dir = ctx.roots.dashboards_dir
    product = getattr(request.app.state, "product", None)
    dashboard = _dashboard_file(dashboards_dir, path, product)
    if dashboard is not None:
        if dashboard.suffix == ".html":
            return _machinery_page_response(dashboard, dashboards_dir, product)
        return FileResponse(dashboard)
    declared = product_file(product, "index.html" if path in {"", "/"} else path)
    if declared is not None:
        return FileResponse(declared)
    raise HTTPException(status_code=404)


_HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})


def _route_label(path: object) -> str | None:
    """A route template safe to use as one Prometheus label, or none."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    if '"' in path or "\n" in path or "\r" in path:
        return None
    return path


def _route_template(scope: Scope) -> str:
    """Return the matched route template, never the request path or query.

    ``scope["route"]`` is the route as declared on its own router. For an
    included router that path is only the suffix, so ``/api/delegate/tasks/{task_id}``
    and ``/api/coordination/tasks/{task_id}`` would share ``/tasks/{task_id}``.
    FastAPI stores this match's prefixed template on
    ``scope["fastapi"]["effective_route_context"]``. That object is the match,
    not the first mount of the route: ``/artifacts`` and ``/files`` include the
    same router. The request path is never a label; a path parameter would
    make the series unbounded. A match with no usable template is ``unmatched``.
    """
    metrics_path = PrometheusMetricsMiddleware.METRICS_PATH
    if scope.get("method") == "GET" and scope.get("path") == metrics_path:
        return metrics_path
    fastapi_scope = scope.get("fastapi")
    if isinstance(fastapi_scope, dict):
        effective = fastapi_scope.get("effective_route_context")
        labeled = _route_label(getattr(effective, "path", None))
        if labeled is not None:
            return labeled
    labeled = _route_label(getattr(scope.get("route"), "path", None))
    if labeled is not None:
        return labeled
    return "unmatched"


def _status_class(status_code: int) -> str:
    hundred = status_code // 100
    if 1 <= hundred <= 5:
        return f"{hundred}xx"
    return "5xx"


_METRICS_REFRESH_S = 5.0


@dataclass(frozen=True, eq=False)
class _CollectedMetrics:
    families: tuple[Metric, ...]
    indexed: bool

    def collect(self) -> tuple[Metric, ...]:
        return self.families

    def describe(self) -> tuple[Metric, ...]:
        return self.families if self.indexed else ()


class PrometheusMetricsMiddleware:
    """Record request totals, latency, and 5xx responses, and serve GET /metrics.

    The lifespan refreshes the Prometheus text 0.0.4 exposition off-loop.
    The route label is the full matched template (router prefix included).

    ``METRICS_PATH`` is the path the route inventory records with FastAPI's
    framework routes. Those paths are served and are outside the OpenAPI
    operation count.
    """

    METRICS_PATH = "/metrics"

    def __init__(self, context: MonitorContext) -> None:
        self._requests = Counter(
            "monitor_http_requests_total",
            "HTTP requests handled by the Monitor API.",
            labelnames=("method", "route", "status"),
            registry=None,
        )
        self._errors = Counter(
            "monitor_http_responses_5xx_total",
            "HTTP responses whose status class is 5xx.",
            labelnames=("method", "route", "status"),
            registry=None,
        )
        self._latency = Histogram(
            "monitor_http_request_duration_seconds",
            "HTTP request latency in seconds.",
            labelnames=("method", "route", "status"),
            registry=None,
        )
        # Preserve exposition order and which collectors the registry indexes for name[].
        self._collectors = (
            (CommsCollector(context), False),
            (ModelStatsCollector(context), False),
            (self._requests, True),
            (self._errors, True),
            (self._latency, True),
            (subscriptions.PoolPaceCollector(), False),
        )
        self.snapshot = PeriodicSnapshot(self._collect_snapshot, _METRICS_REFRESH_S, dormant_wait_s=0)

    def _collect_snapshot(self) -> tuple[bytes, CollectorRegistry]:
        registry = CollectorRegistry()
        for collector, indexed in self._collectors:
            registry.register(_CollectedMetrics(tuple(collector.collect()), indexed))
        return generate_latest(registry), registry

    def _scrape(self, scope: Scope) -> Response:
        try:
            (body, registry), age = self.snapshot.read()
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        query = scope.get("query_string", b"")
        if query and (names := parse_qs(query.decode("utf-8")).get("name[]")):
            body = generate_latest(registry.restricted_registry(names))
        return Response(body, headers={
            "Content-Type": CONTENT_TYPE_PLAIN_0_0_4,
            "X-Snapshot-Age-S": f"{age:.3f}",
        })

    def wrap(self, app: ASGIApp) -> ASGIApp:
        return functools.partial(self._instrument, app)

    async def _instrument(self, app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            if scope.get("method") == "GET" and scope.get("path") == self.METRICS_PATH:
                await self._scrape(scope)(scope, receive, send_wrapper)
            else:
                await app(scope, receive, send_wrapper)
        finally:
            self._record(scope, status_code, time.perf_counter() - started)

    def _record(self, scope: Scope, status_code: int, elapsed: float) -> None:
        method = scope.get("method")
        if method not in _HTTP_METHODS:
            method = "OTHER"
        status = _status_class(status_code)
        labels = {"method": method, "route": _route_template(scope), "status": status}
        self._requests.labels(**labels).inc()
        self._latency.labels(**labels).observe(max(elapsed, 0.0))
        if status == "5xx":
            self._errors.labels(**labels).inc()


def create_app(
    context: MonitorContext,
    *,
    lifespan: Any = None,
    product: ProductContribution | None = None,
) -> FastAPI:
    """Build a fresh Ops API app bound to one context.

    ``product`` is the product contribution; when omitted it is loaded through
    the configured product entry (``load_product``). Its routers are included
    ahead of the machinery routers and the core catch-all stays last.

    A supplied ``lifespan`` replaces the default, including metrics collection.
    """
    metrics = PrometheusMetricsMiddleware(context)

    @asynccontextmanager
    async def lifespan_with_metrics(app: FastAPI):
        async with _lifespan(app) as state, app.state.metrics_snapshot.lifespan():
            yield state

    product = load_product(context) if product is None else product
    factory_app = FastAPI(
        title="Ops API",
        version="2.0.0",
        description=(
            "Operator API (Ops API) for the Ukrainian curriculum pipeline. "
            "Application version 2.0.0 (also on GET /api/health and /api/config). "
            "Stream key remains `monitor`; historic Monitor docs paths are progressive rename. "
            "Versioning layers: app 2.0.0 · URL namespaces `/api/*/v1/*` · "
            "payload `response_schema_version` / `schema_version` on contracts. "
            "Agent onboard: GET /api/rules, GET /api/orient?lean=true. "
            "Interactive explorer: /docs (Swagger) and /redoc. "
            "Powers ops dashboards (root /), cold-start, state, comms, delegate, "
            "build events, and operational tooling."
        ),
        lifespan=lifespan_with_metrics if lifespan is None else lifespan,
    )
    factory_app.state.ctx = context
    factory_app.state.product = product
    factory_app.state.metrics_snapshot = metrics.snapshot
    install_providers(product)
    factory_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    factory_app.middleware("http")(resilience_middleware)
    factory_app.middleware("http")(opsec_path_sanitizer_middleware)
    factory_app.add_middleware(metrics.wrap)
    factory_app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    factory_app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    factory_app.add_exception_handler(Exception, global_exception_handler)

    # Product routers first, then the machinery routers in their established
    # order. The core router must remain last so its catch-all route keeps
    # today's matching precedence.
    for mount in product.routers:
        factory_app.include_router(mount.router, prefix=mount.prefix, tags=list(mount.tags) or None)
    factory_app.include_router(occupancy_router, prefix="/api/occupancy", tags=["occupancy"])
    factory_app.include_router(observer_presence_router, prefix="/api/observer", tags=["observer"])
    factory_app.include_router(epics_router, prefix="/api/epics", tags=["epics"])
    factory_app.include_router(comms_router, prefix="/api/comms")
    factory_app.include_router(fleet_router, prefix="/api/fleet", tags=["fleet"])
    factory_app.include_router(project_state_router, prefix="/api/fleet", tags=["fleet"])
    factory_app.include_router(fleet_workers_router, prefix="/api/fleet", tags=["fleet"])
    factory_app.include_router(model_stats_router, prefix="/api/fleet/model-stats", tags=["fleet"])
    factory_app.include_router(session_streams_router, prefix="/api/session-streams", tags=["session-streams"])
    factory_app.include_router(coordination_router, prefix="/api/coordination")
    factory_app.include_router(cluster_router, prefix="/api/cluster", tags=["cluster"])
    factory_app.include_router(delegate_router, prefix="/api/delegate")
    factory_app.include_router(docs_router, prefix="/artifacts")
    factory_app.include_router(docs_router, prefix="/files")
    factory_app.include_router(milestones_router, prefix="/api/milestones", tags=["milestones"])
    factory_app.include_router(git_hygiene_router, prefix="/api/git", tags=["git"])
    factory_app.include_router(ops_router, prefix="/api/ops", tags=["ops"])
    factory_app.include_router(issues_router, prefix="/api/issues", tags=["issues"])
    factory_app.include_router(knowledge_router, prefix="/api/knowledge", tags=["knowledge"])
    factory_app.include_router(rules_router, prefix="/api/rules", tags=["rules"])
    factory_app.include_router(runtime_router, prefix="/api/runtime")
    factory_app.include_router(session_router, prefix="/api/session", tags=["session"])
    factory_app.include_router(state_router, prefix="/api/state")
    factory_app.include_router(telemetry_router)
    factory_app.include_router(worktrees_router, prefix="/api/worktrees", tags=["worktrees"])
    factory_app.include_router(work_router, prefix="/api/work", tags=["work"])
    factory_app.include_router(core_router)
    return factory_app


def _product_data_present() -> bool:
    """The selected product checkout holds the product entry data; no product checkout means no."""
    try:
        monitor_paths = config.PRODUCT_ROOT / PRODUCT_DATA
    except ProductRootError:
        return False
    return not monitor_paths.is_symlink() and monitor_paths.is_file()


# Build the production app at import when the product checkout (product_root, C-4746)
# holds its product data. An extracted machinery tree has no product checkout, so import
# stays product-free and the first use of app builds it. A missing product checkout or
# file still refuses that build.
if _product_data_present():
    app = create_app(production_context())
else:

    def __getattr__(name: str) -> Any:
        if name != "app":
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        application = create_app(production_context())
        globals()["app"] = application
        return application
