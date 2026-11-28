"""Read-only Monitor API surface for the parallel-agent ledger."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from lu_machinery.orchestration import agent_ledger

from .monitor_context import MonitorContext, get_ctx, resolve_context
from .runtime_router import local_body_read_access_refusal

router = APIRouter(tags=["coordination"])

# Scalar fields any caller may read. Paths, thread IDs, metadata, events and the
# lifecycle carrier stay in the full record, which only a local caller receives.
SUMMARY_FIELDS = (
    "schema_version",
    "task_id",
    "issue",
    "lane",
    "module_family",
    "task_family",
    "agent",
    "model",
    "status",
    "pr_url",
    "heartbeat_at",
    "created_at",
    "updated_at",
)


def _state_root(ctx: MonitorContext | None = None) -> Path:
    return resolve_context(ctx).roots.state_root


def full_detail_authorized(request: Request) -> bool:
    """True only when the shared local body-read guard accepts the request."""
    return local_body_read_access_refusal(request) is None


def task_summary(task: dict[str, Any]) -> dict[str, Any]:
    """The allowlisted projection of one ledger record, with counts in place of its lists."""
    summary = {field: task.get(field) for field in SUMMARY_FIELDS}
    owned = task.get("owned_paths")
    events = task.get("events")
    summary["owned_paths_count"] = len(owned) if isinstance(owned, list) else 0
    summary["events_count"] = len(events) if isinstance(events, list) else 0
    summary["detail"] = "summary"
    return summary


@router.get("/tasks/{task_id}")
async def coordination_task(
    task_id: str,
    ctx: MonitorContext = Depends(get_ctx),
    full_detail: bool = Depends(full_detail_authorized),
) -> dict[str, Any]:
    """Return one ledger task: the full record to a local caller, otherwise its summary."""
    try:
        task = await asyncio.to_thread(agent_ledger.get_task, _state_root(ctx), task_id)
    except agent_ledger.LedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    # A direct call that omits the dependency gets the Depends marker, never full detail.
    return task if full_detail is True else task_summary(task)
